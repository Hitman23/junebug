import collections
from copy import deepcopy
import json
import uuid
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.web import http
from vumi.message import TransportUserMessage
from vumi.service import WorkerCreator
from vumi.servicemaker import VumiOptions

from junebug.error import JunebugError


class ChannelNotFound(JunebugError):
    '''Raised when a channel's data cannot be found.'''
    name = 'ChannelNotFound'
    description = 'channel not found'
    code = http.NOT_FOUND


class InvalidChannelType(JunebugError):
    '''Raised when an invalid channel type is specified'''
    name = 'InvalidChannelType',
    description = 'invalid channel type'
    code = http.BAD_REQUEST


transports = {
    'telnet': 'vumi.transports.telnet.TelnetServerTransport',
    'xmpp': 'vumi.transports.xmpp.XMPPTransport',
}

allowed_message_fields = [
    'transport_name', 'timestamp', 'in_reply_to', 'to_addr', 'from_addr',
    'content', 'session_event', 'helper_metadata', 'message_id']
# excluded fields: from_addr_type, group, provider, routing_metadata,
# to_addr_type, from_addr_type, message_version, transport_metadata,
# message_type, transport_type


class Channel(object):
    def __init__(
            self, redis_manager, amqp_config, properties, id=None):
        '''Creates a new channel. ``redis_manager`` is the redis manager, from
        which a sub manager is created using the channel id. If the channel id
        is not supplied, a UUID one is generated. Call ``save`` to save the
        channel data. It can be started using the ``start`` function.'''
        self._properties, self.id, self.redis = (
            properties, id, redis_manager)
        if self.id is None:
            self.id = str(uuid.uuid4())

        self.options = deepcopy(VumiOptions.default_vumi_options)
        self.options.update(amqp_config)

        self.transport_worker = None
        self.application_worker = None

    def start(self, service, transport_worker=None):
        '''Starts the relevant workers for the channel. ``service`` is the
        parent of under which the workers should be started.'''
        self._start_transport(service, transport_worker)
        self._start_application(service)

    @inlineCallbacks
    def stop(self):
        '''Stops the relevant workers for the channel'''
        yield self._stop_application()
        yield self._stop_transport()

    @inlineCallbacks
    def save(self):
        '''Saves the channel data into redis.'''
        properties = json.dumps(self._properties)
        channel_redis = yield self.redis.sub_manager(self.id)
        yield channel_redis.set('properties', properties)
        yield self.redis.sadd('channels', self.id)

    @inlineCallbacks
    def update(self, properties):
        '''Updates the channel configuration, saves the updated configuration,
        and (if needed) restarts the channel with the new configuration.
        Returns the updated configuration and status.'''
        self._properties.update(properties)
        yield self.save()

        # Only restart if the channel config has changed
        if properties.get('config') is not None:
            service = self.transport_worker.parent
            yield self.stop()
            yield self.start(service)

        returnValue((yield self.status()))

    @inlineCallbacks
    def delete(self):
        '''Removes the channel data from redis'''
        channel_redis = yield self.redis.sub_manager(self.id)
        yield channel_redis.delete('properties')

    def status(self):
        '''Returns a dict with the configuration and status of the channel'''
        status = deepcopy(self._properties)
        status['id'] = self.id
        # TODO: Implement channel status
        status['status'] = {}
        return status

    @classmethod
    @inlineCallbacks
    def from_id(cls, redis, amqp_config, id, parent):
        '''Creates a channel by loading the data from redis, given the
        channel's id, and the parent service of the channel'''
        channel_redis = yield redis.sub_manager(id)
        properties = yield channel_redis.get('properties')
        if properties is None:
            raise ChannelNotFound()
        properties = json.loads(properties)

        obj = cls(redis, amqp_config, properties, id)
        obj._restore(parent)

        returnValue(obj)

    @classmethod
    @inlineCallbacks
    def get_all(cls, redis):
        '''Returns a set of keys of all of the channels'''
        channels = yield redis.smembers('channels')
        returnValue(channels)

    @classmethod
    @inlineCallbacks
    def send_message(cls, id, message_sender, msg):
        '''Sends a message. Takes a junebug.amqp.MessageSender instance to
        send a message.'''
        message = TransportUserMessage.send(
            **cls.message_from_api(id, msg))
        queue = '%s.outbound' % id
        msg = yield message_sender.send_message(message, routing_key=queue)
        returnValue(cls.api_from_message(msg))

    @classmethod
    def api_from_message(cls, msg):
        ret = {}
        ret['to'] = msg['to_addr']
        ret['from'] = msg['from_addr']
        ret['message_id'] = msg['message_id']
        ret['channel_id'] = msg['transport_name']
        ret['timestamp'] = msg['timestamp']
        ret['reply_to'] = msg['in_reply_to']
        ret['content'] = msg['content']
        ret['channel_data'] = msg['helper_metadata']
        if msg.get('continue_session') is not None:
            ret['channel_data']['continue_session'] = msg['continue_session']
        if msg.get('session_event') is not None:
            ret['channel_data']['session_event'] = msg['session_event']
        return ret

    @classmethod
    def message_from_api(cls, id, msg):
        ret = {}
        ret['to_addr'] = msg.get('to')
        ret['from_addr'] = msg['from']
        ret['content'] = msg['content']
        ret['transport_name'] = id
        channel_data = msg.get('channel_data', {})
        if channel_data.get('continue_session') is not None:
            ret['continue_session'] = channel_data.pop('continue_session')
        if channel_data.get('session_event') is not None:
            ret['session_event'] = channel_data.pop('session_event')
        ret['helper_metadata'] = channel_data
        return ret

    @property
    def _application_id(self):
        return 'application:%s' % (self.id,)

    @property
    def _transport_config(self):
        config = self._properties['config']
        config = self._convert_unicode(config)
        config['transport_name'] = self.id
        return config

    @property
    def _application_config(self):
        return {
            'transport_name': self.id,
            'mo_message_url': self._properties['mo_url'],
        }

    @property
    def _transport_cls_name(self):
        cls_name = transports.get(self._properties.get('type'))

        if cls_name is None:
            raise InvalidChannelType(
                'Invalid channel type %r, must be one of: %s' % (
                    self._properties.get('type'),
                    ', '.join(transports.keys())))

        return cls_name

    @property
    def _application_cls_name(self):
        return 'junebug.workers.MessageForwardingWorker'

    def _start_transport(self, service, transport_worker):
        # transport_worker parameter is for testing, if it is None,
        # create the transport worker
        if transport_worker is None:
            transport_worker = self._create_transport()

        transport_worker.setName(self.id)
        transport_worker.setServiceParent(service)
        self.transport_worker = transport_worker

    def _start_application(self, service):
        worker = self._create_application()
        worker.setName(self._application_id)
        worker.setServiceParent(service)
        self.application_worker = worker

    def _create_transport(self):
        return self._create_worker(
            self._transport_cls_name,
            self._transport_config)

    def _create_application(self):
        return self._create_worker(
            self._application_cls_name,
            self._application_config)

    def _create_worker(self, cls_name, config):
        creator = WorkerCreator(self.options)
        worker = creator.create_worker(cls_name, config)
        return worker

    @inlineCallbacks
    def _stop_transport(self):
        if self.transport_worker is not None:
            yield self.transport_worker.disownServiceParent()
            self.transport_worker = None

    @inlineCallbacks
    def _stop_application(self):
        if self.application_worker is not None:
            yield self.application_worker.disownServiceParent()
            self.application_worker = None

    def _restore(self, service):
        self.transport_worker = service.getServiceNamed(self.id)
        self.application_worker = service.getServiceNamed(self._application_id)

    def _convert_unicode(self, data):
        # Twisted doesn't like it when we give unicode in for config things
        if isinstance(data, basestring):
            return str(data)
        elif isinstance(data, collections.Mapping):
            return dict(map(self._convert_unicode, data.iteritems()))
        elif isinstance(data, collections.Iterable):
            return type(data)(map(self._convert_unicode, data))
        else:
            return data
