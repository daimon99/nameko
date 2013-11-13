import socket

import eventlet
from eventlet.event import Event
from kombu import Queue, Exchange, Connection
from kombu.exceptions import TimeoutError
from mock import patch, Mock, ANY

from nameko.messaging import QueueConsumer, AMQP_URI_CONFIG_KEY

import pytest

TIMEOUT = 5

exchange = Exchange('spam')
ham_queue = Queue('ham', exchange=exchange, auto_delete=False)


class MessageHandler(object):
    queue = ham_queue

    def __init__(self):
        self.handle_message_called = Event()

    def handle_message(self, body, message):
        self.handle_message_called.send(message)

    def wait(self):
        return self.handle_message_called.wait()


def test_lifecycle(reset_rabbit, rabbit_manager, rabbit_config):

    container = Mock()
    container.config = rabbit_config
    container.max_workers = 3
    container.spawn_managed_thread.side_effect = eventlet.spawn

    queue_consumer = QueueConsumer()
    queue_consumer.bind("queue_consumer", container)

    handler = MessageHandler()

    queue_consumer.register_provider(handler)

    queue_consumer.start()

    # making sure the QueueConsumer uses the container to spawn threads
    container.spawn_managed_thread.assert_called_once_with(ANY)

    vhost = rabbit_config['vhost']
    rabbit_manager.publish(vhost, 'spam', '', 'shrub')

    message = handler.wait()

    gt = eventlet.spawn(queue_consumer.unregister_provider, handler)

    # wait for the handler to be removed
    with eventlet.Timeout(TIMEOUT):
        while len(queue_consumer._consumers):
            eventlet.sleep()

    # remove_consumer has to wait for all messages to be acked
    assert not gt.dead

    # the consumer should have stopped and not accept any new messages
    rabbit_manager.publish(vhost, 'spam', '', 'ni')

    # this should cause the consumer to finish shutting down
    queue_consumer.ack_message(message)
    with eventlet.Timeout(TIMEOUT):
        gt.wait()

    # there should be a message left on the queue
    messages = rabbit_manager.get_messages(vhost, 'ham')
    assert ['ni'] == [msg['payload'] for msg in messages]


def test_reentrant_start_stops(reset_rabbit, rabbit_config):
    container = Mock()
    container.config = rabbit_config
    container.max_workers = 3
    container.spawn_managed_thread = eventlet.spawn

    queue_consumer = QueueConsumer()
    queue_consumer.bind("queue_consumer", container)

    queue_consumer.start()
    gt = queue_consumer._gt

    # nothing should happen as the consumer has already been started
    queue_consumer.start()
    assert gt is queue_consumer._gt


def test_stop_while_starting():
    started = Event()

    container = Mock()
    container.config = {AMQP_URI_CONFIG_KEY: None}
    container.max_workers = 3
    container.spawn_managed_thread = eventlet.spawn

    class BrokenConnConsumer(QueueConsumer):
        def consume(self, *args, **kwargs):
            started.send(None)
            # kombu will retry again and again on broken connections
            # so we have to make sure the event is reset to allow consume
            # to be called again
            started.reset()
            return super(BrokenConnConsumer, self).consume(*args, **kwargs)

    queue_consumer = BrokenConnConsumer()
    queue_consumer.bind("queue_consumer", container)

    handler = MessageHandler()
    queue_consumer.register_provider(handler)

    with eventlet.Timeout(TIMEOUT):
        with patch.object(Connection, 'connect') as connect:
            # patch connection to raise an error
            connect.side_effect = TimeoutError('test')
            # try to start the queue consumer
            gt = eventlet.spawn(queue_consumer.start)
            # wait for the queue consumer to begin starting and
            # then immediately stop it
            started.wait()

    with eventlet.Timeout(TIMEOUT):
        queue_consumer.unregister_provider(handler)
        queue_consumer.stop()

    with eventlet.Timeout(TIMEOUT):
        # we expect the queue_consumer.start thread to finish
        # almost immediately adn when it does the queue_consumer thread
        # should be dead too
        while not gt.dead:
            eventlet.sleep()

        assert queue_consumer._gt.dead


def test_error_stops_consumer_thread():
    container = Mock()
    container.config = {AMQP_URI_CONFIG_KEY: None}
    container.max_workers = 3
    container.spawn_managed_thread = eventlet.spawn

    queue_consumer = QueueConsumer()
    queue_consumer.bind("queue_consumer", container)

    handler = MessageHandler()
    queue_consumer.register_provider(handler)

    with eventlet.Timeout(TIMEOUT):
        with patch.object(Connection, 'drain_events') as drain_events:
            drain_events.side_effect = Exception('test')
            queue_consumer.start()

    with pytest.raises(Exception) as exc_info:
        queue_consumer._gt.wait()

    assert exc_info.value.args == ('test',)


def test_socket_error_kills_consumer():
    container = Mock()
    container.config = {AMQP_URI_CONFIG_KEY: None}
    container.max_workers = 1
    container.spawn_managed_thread = eventlet.spawn

    queue_consumer = QueueConsumer()

    queue_consumer.bind("queue_consumer", container)

    handler = MessageHandler()
    queue_consumer.register_provider(handler)
    queue_consumer.start()

    with patch.object(Connection, 'drain_events') as drain_events:
        drain_events.side_effect = socket.error('test-error')
        # for now we are happy with socket errors to just kill the consumer
        # in the future we want retries to work
        with pytest.raises(Exception):
            queue_consumer._gt.wait()


def test_prefetch_count(reset_rabbit, rabbit_manager, rabbit_config):
    container = Mock()
    container.config = rabbit_config
    container.max_workers = 1
    container.spawn_managed_thread = eventlet.spawn

    queue_consumer1 = QueueConsumer()
    queue_consumer1.bind("queue_consumer", container)

    queue_consumer2 = QueueConsumer()
    queue_consumer2.bind("queue_consumer", container)

    consumer_continue = Event()

    class Handler1(object):
        queue = ham_queue

        def handle_message(self, body, message):
            consumer_continue.wait()
            queue_consumer1.ack_message(message)

    messages = []

    class Handler2(object):
        queue = ham_queue

        def handle_message(self, body, message):
            messages.append(body)
            queue_consumer2.ack_message(message)

    handler1 = Handler1()
    handler2 = Handler2()

    queue_consumer1.register_provider(handler1)
    queue_consumer2.register_provider(handler2)

    queue_consumer1.start()
    queue_consumer2.start()

    vhost = rabbit_config['vhost']
    # the first consumer only has a prefetch_count of 1 and will only
    # consume 1 message and wait in handler1()
    rabbit_manager.publish(vhost, 'spam', '', 'ham')
    # the next message will go to handler2() no matter of any prefetch_count
    rabbit_manager.publish(vhost, 'spam', '', 'eggs')
    # the third message is only going to handler2 because the first consumer
    # has a prefetch_count of 1 and thus is unable to deal with another message
    # until having ACKed the first one
    rabbit_manager.publish(vhost, 'spam', '', 'bacon')

    with eventlet.Timeout(TIMEOUT):
        while len(messages) < 2:
            eventlet.sleep()

    # allow the waiting consumer to ack its message
    consumer_continue.send(None)

    assert messages == ['eggs', 'bacon']

    queue_consumer1.unregister_provider(handler1)
    queue_consumer2.unregister_provider(handler2)


def test_kill_connections_close(reset_rabbit, rabbit_manager, rabbit_config):
    container = Mock()
    container.config = rabbit_config
    container.max_workers = 1
    container.spawn_managed_thread = eventlet.spawn

    queue_consumer = QueueConsumer()
    queue_consumer.bind("queue_consumer", container)

    class Handler(object):
        queue = ham_queue

        def handle_message(self, body, message):
            pass

    queue_consumer.register_provider(Handler())
    queue_consumer.start()

    # kill should close all connections
    queue_consumer.kill(Exception('test-kill'))

    connections = rabbit_manager.get_connections()
    assert connections is None
