from functools import partial
from inspect import isgenerator

from pulsar.utils.pep import itervalues

from .defer import Deferred, maybe_async
from .access import logger

__all__ = ['EventHandler', 'Event']


class Event(object):
    _pause_counter = 0
    
    def bind(self, callback, errback=None):
        '''Bind a ``callback`` for ``caller`` to this :class:`Event`.'''
        pass
    
    def has_fired(self):
        '''Check if this event has fired.

        This only make sense for one time events.
        '''
        return True
    
    def fire(self, arg, callback=None, errback=None, **kwargs):
        '''Fire this event'''
        raise NotImplementedError
    
    def chain(self, event):
        '''Chain another ``event`` with this event.'''
        raise NotImplementedError
    
    def pause(self):
        if not self._pause_counter:
            self._pause_counter = 1
        else:
            self._pause_counter += 1


class ManyEvent(Event):
    '''An event managed by an :class:`EventHandler` class.'''
    def __init__(self, name):
        self.name = name
        self._handlers = []
    
    def __repr__(self):
        return repr(self._handlers)
    __str__ = __repr__
    
    def bind(self, callback, errback=None):
        assert errback == None, 'errback not supported in many-times events'
        self._handlers.append(callback)
    
    def fire(self, arg, callback=None, errback=None, **kwargs):
        if self._pause_counter:
            self._pause_counter -= 1
            return
        for hnd in self._handlers:
            try:
                g = hnd(arg, **kwargs)
            except Exception:
                logger().exception('Exception while firing "%s" '
                                   'event for %s', self.name, arg)
            else:
                if isgenerator(g):
                    # Add it to the event loop
                    maybe_async(g)
    
    def pause(self):
        if not self.has_fired():
            if not self._pause_counter:
                self._pause_counter = 1
            else:
                self._pause_counter += 1


class OneTime(Deferred, Event):
    
    def __init__(self, name):
        super(OneTime, self).__init__()
        self.name = name
        self._events = Deferred()
        
    def bind(self, callback, errback=None):
        self._events.add_callback(callback, errback)
    
    def has_fired(self):
        return self._events.done()
        
    def fire(self, arg, callback=None, errback=None, **kwargs):
        if self._pause_counter:
            self._pause_counter -= 1
            return
        if self._events.done():
            logger().warning('Event "%s" already fired for %s', self.name, arg)
        else:
            assert not kwargs, ("One time events can don't support key-value "
                                "parameters")
            if callback:
                self.add_callback(callback, errback)
            result = self._events.callback(arg)
            if isinstance(result, Deferred):
                # a deferred, add a check at the end of the callback pile
                self._events.add_callback(self._check, self._check)
                return self
            elif self._chained_to is None:
                return self.callback(result)
        
    def chain(self, event):
        '''Chain ``event`` to this ``event`.'''
        if isinstance(event, OneTime):
            if not event.has_fired():
                self.add_callback(event.fire, event.fire)
            elif not event.done():
                super(OneTime, self).chain(event)
    
    def _check(self, result):
        if self._events._callbacks:
            # other callbacks have been added,
            # put another check at the end of the pile
            self._events.add_callback(self._check)
        elif self._chained_to is None:
            self.callback(result)
            
            
            
class EventHandler(object):
    '''A Mixin for handling events.

    It handles one time events and events that occur several
    times. This mixin is used in :class:`Protocol` and :class:`Producer`
    for scheduling connections and requests.
    '''
    ONE_TIME_EVENTS = ()
    '''Event names which occur once only.'''
    MANY_TIMES_EVENTS = ()
    '''Event names which occur several times.'''
    def __init__(self, one_time_events=None, many_times_events=None):
        one = self.ONE_TIME_EVENTS
        if one_time_events:
            one = set(one)
            one.update(one_time_events)
        events = dict(((e, OneTime(e)) for e in one))
        many = self.MANY_TIMES_EVENTS
        if many_times_events:
            many = set(many)
            many.update(many_times_events)
        events.update(((e, ManyEvent(e)) for e in many))
        self._events = events

    @property
    def events(self):
        return self._events
        
    def event(self, name):
        '''Return the :class:`Event` for ``name``.'''
        return self._events.get(name)
        
    def bind_event(self, event, callback, errback=None):
        '''Register a ``callback`` with ``event``.

        **The callback must be a callable which accept one parameter**,
        the instance firing the event or the first positional argument
        passed to the :meth:`fire_event` method.

        :param event: the event name. If the event is not available a warning
            message is logged.
        :param callback: a callable receiving two positional parameters.
        '''
        if event in self._events:
            self._events[event].bind(callback, errback)
        else:
            logger().warning('Unknown event "%s" for %s', event, self)
    
    def bind_events(self, **events):
        '''Register all known events found in ``events`` key-valued parameters.
        '''
        for name in self._events:
            if name in events:
                self.bind_event(name, events[name])
    
    def fire_event(self, name, arg=None, callback=None,
                   errback=None, **kwargs):
        """Dispatches ``arg`` or ``self`` to event ``name`` listeners.

        * If event at ``name`` is a one-time event, it makes sure that it was
          not fired before.
        
        :param arg: optional argument passed as second parameter to the
            event handler.
        :return: boolean indicating if the event was fired or not.
        """
        if arg is None:
            arg = self
        if name in self._events:
            return self._events[name].fire(arg, callback, errback, **kwargs)
        elif warning:
            logger().warning('Unknown event "%s" for %s', name, self)
            return callback(arg) if arg else arg
    
    def pause_event(self, name):
        '''Pause event ``name``.
        
        This causes the event not to fire at the next :meth:`fire`
        call for event ``name``.
        '''
        event = self._events.get(name)
        if event:
            event.pause()
    
    def chain_event(self, other, name):
        '''Chain the event ``name`` from ``other``.
        
        :param other: an :class:`EventHandler` to chain to.
        :param name: event name to chain.
        '''
        event = self._events.get(name)
        if event and isinstance(other, EventHandler):
            event2 = other._events.get(name)
            if event2:
                event.chain(event2)
    
    def cancel_one_time_events(self, exclude=None):
        '''Cancel all one time events not already fired.'''
        exclude = exclude or ()
        for event in itervalues(self._events):
            if isinstance(event, OneTime) and event.name not in exclude:
                event.cancel(mute=True)
        
    def copy_many_times_events(self, other):
        '''Copy :ref:`many times events <many-times-event>` from  ``other``.
        
        All many times events of ``other`` are copied to this handler
        provided the events handlers already exist.
        '''
        if isinstance(other, EventHandler):
            events = self._events
            for event in itervalues(other._events):
                if isinstance(event, ManyEvent):
                    ev = events.get(event.name)
                    # If the event is available add it
                    if ev:
                        for callback in event._handlers:
                            ev.bind(callback)
        