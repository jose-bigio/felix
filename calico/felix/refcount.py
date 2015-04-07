# Copyright (c) Metaswitch Networks 2015. All rights reserved.
import collections
import functools

import logging
import weakref
from calico.felix.actor import Actor, actor_event
import gevent

_log = logging.getLogger(__name__)

# States that a reference-counted actor can be in.

# Initial state, created but not yet started.  May stay in this state if
# we're waiting for a previous actor with same ID to clean up.
CREATED = "created"
# We told the actor to start but haven't heard back from it via
# on_object_startup_complete() yet.
STARTING = "starting"
# We've heard back from the actor, it's live and ready to be distributed to
# referrers.
LIVE = "live"
# We've told the actor to stop, there are no more references to it in the wild
# and we'll never activate it again.
STOPPING = "stopping"


class ReferenceManager(Actor):
    """
    Actor that manages the life cycle of a collection of other Actors
    by name. Users can request a reference to an actor by name using
    get_and_incref() and then they must return it by calling decref().

    Consecutive calls to incref return the same actor. Actors are only
    cleaned up when their reference count hits zero.

    Users who obtain a reference through get_and_incref() must stop
    using the reference before calling decref().
    """

    def __init__(self, qualifier=None):
        super(ReferenceManager, self).__init__(qualifier=qualifier)
        self.objects_by_id = {}
        self.stopping_objects_by_id = collections.defaultdict(set)
        self.pending_ref_callbacks = collections.defaultdict(set)

    @actor_event
    def get_and_incref(self, object_id, callback=None):
        """
        Acquire a reference to a ref-counted Actor, returns via callback.
        :param object_id: opaque ID of the Actor to retrieve, must be hashable.
        :param callback: callback, receives the object_id and object as args.
        """
        _log.debug("Request for object %s", object_id)
        assert object_id is not None

        if object_id not in self.objects_by_id:
            _log.debug("%s object with id %s didn't exist",
                       self.name, object_id)
            obj = self._create(object_id)
            obj._manager = weakref.proxy(self)
            obj._id = object_id
            self.objects_by_id[object_id] = obj
        else:
            obj = self.objects_by_id[object_id]
            _log.debug("%s object with id %s existed with ref count %d",
                       self.name, object_id, obj.ref_count)

        if callback:
            self.pending_ref_callbacks[object_id].add(callback)
        obj.ref_count += 1

        # Depending on state of object, may need to start it or immediately
        # call back.
        self._maybe_start(object_id)
        self._maybe_notify_referrers(object_id)

    @actor_event
    def on_object_startup_complete(self, object_id, obj):
        """
        Callback from a ref-counted object to tell us that it has completed
        its startup.

        The ref-counted actor must make this callback once it is ready to
        be referenced unless it receives an on_unreferenced() message,
        after which calls to this method from that actor are allowed but
        ignored.
        """
        _log.debug("Object startup complete for %s", object_id)
        if self.objects_by_id.get(object_id) is not obj:
            _log.info("Ignoring on_object_startup_complete for old instance")
            return
        if obj.ref_mgmt_state != STARTING:
            _log.info("Ignoring on_object_startup_complete for instance "
                      "in state %s", obj.ref_mgmt_state)
            return
        obj.ref_mgmt_state = LIVE
        self._maybe_notify_referrers(object_id)

    @actor_event
    def decref(self, object_id):
        """
        Return a reference and garbage-collect the backing actor if it is no
        longer referenced elsewhere.
        """
        assert object_id in self.objects_by_id
        obj = self.objects_by_id[object_id]
        obj.ref_count -= 1
        assert obj.ref_count >= 0, "Ref count dropped below 0.s"
        _log.debug("Reference count for %s object %s is %d",
                   self.name, object_id, obj.ref_count)
        if obj.ref_count == 0:
            _log.debug("No more references to object with id %s", object_id)
            if obj.ref_mgmt_state == CREATED:
                _log.debug("%s was never started, discarding", obj)
            else:
                _log.debug("%s is running, cleaning it up", obj)
                obj.ref_mgmt_state = STOPPING
                obj.on_unreferenced(async=True)
                self.stopping_objects_by_id[object_id].add(obj)
            self.objects_by_id.pop(object_id)
            self.pending_ref_callbacks.pop(object_id, None)

    @actor_event
    def on_object_cleanup_complete(self, object_id, obj):
        """
        Callback from ref-counted actor to tell us that it has finished
        its cleanup and it is safe to clean up our state and start new
        instances with the same ID.
        """
        _log.debug("Cleanup complete for %s, removing it from map", obj)
        self.stopping_objects_by_id[object_id].discard(obj)
        if not self.stopping_objects_by_id[object_id]:
            del self.stopping_objects_by_id[object_id]
            # May have unblocked start of new object...
            self._maybe_start(object_id)

    def _maybe_start(self, obj_id):
        """
        Starts the actor with the given ID if it is present and there
        are no pending cleanups for that ID.
        """
        _log.debug("Checking whether we can start object %s", obj_id)
        obj = self.objects_by_id.get(obj_id)
        if (obj and
                obj.ref_mgmt_state == CREATED and
                obj_id not in self.stopping_objects_by_id):
            _log.debug("Starting object %s", obj_id)
            obj.ref_mgmt_state = STARTING
            obj.start()
            self._on_object_started(obj_id, obj)

    def _maybe_notify_referrers(self, object_id):
        """
        If the object with the given ID is LIVE, notify any pending referrers.
        """
        _log.debug("Checking whether we can notify referrers for %s",
                   object_id)
        obj = self.objects_by_id.get(object_id)
        if obj and obj.ref_mgmt_state == LIVE:
            _log.debug("Object %s is LIVE, notifying referrers", object_id)
            for cb in self.pending_ref_callbacks[object_id]:
                gevent.spawn(cb, object_id, obj)
            self.pending_ref_callbacks.pop(object_id)
        else:
            _log.debug("Cannot notify referrers for %s; object state: %s",
                       object_id, obj.ref_mgmt_state)

    def _on_object_started(self, obj_id, obj):
        """
        To be overriden by subclasses, called to initialize the actor
        after it has been started but before it is returned to referrers.

        This method should set in motion whatever messages need to be sent to
        eventually trigger a call to on_object_startup_complete().
        """
        raise NotImplementedError()  # pragma nocover

    def _create(self, object_id):
        """
        To be overriden by subclasses.

        :returns: A new instance of the actor that this manager is to track.
                  The instance should not be started.
        """
        raise NotImplementedError()  # pragma nocover

    def _is_starting_or_live(self, obj_id):
        return (obj_id in self.objects_by_id
                and self.objects_by_id[obj_id].ref_mgmt_state in
                    (STARTING, LIVE))


class RefHelper(object):
    """
    Helper class for a client of a ReferenceManager.  Manages the
    lifecycle of a set of references and provides a callback when
    all required references are available.
    """
    def __init__(self, actor, ref_mgr, ready_callback):
        self._actor = actor
        """Actor that we belong to, we'll use its queue for callbacks."""
        self._ref_mgr = ref_mgr
        """Ref manager to acquire references from."""
        self._ready_callback = ready_callback
        """Callback to issue when we acquire all the references required."""

        self.required_refs = set()
        """Set of IDs of the references that we've been asked for."""
        self.pending_increfs = set()
        """
        Set of IDs of references for which we have an outstanding incref
        request.
        """
        self.acquired_refs = {}
        """
        Mapping from object ID to object that we've acquired.
        """

    def acquire_ref(self, obj_id):
        """
        Add the given ID to the set of objects that we want to acquire.
        Idempotent; does nothing if the ID is already in the set.
        """
        if obj_id not in self.required_refs:
            # Immediately record that we require this ref.
            self.required_refs.add(obj_id)
            if obj_id not in self.pending_increfs:
                # We're not already asking for the ref, request it.
                _log.debug("Increffing object %s", obj_id)
                cb = functools.partial(self.on_ref_acquired, async=True)
                self.pending_increfs.add(obj_id)
                self._ref_mgr.get_and_incref(obj_id, callback=cb, async=True)

    def discard_ref(self, obj_id):
        """
        Discard the reference identified by ID.  Idempotent; does nothing
        if the reference wasn't present.
        """
        if obj_id in self.required_refs:
            _log.debug("Discarding object %s", obj_id)
            # Immediately record that we no longer want the ref and throw it
            # away.
            self.required_refs.remove(obj_id)
            self.acquired_refs.pop(obj_id, None)
            if obj_id not in self.pending_increfs:
                # We're not still waiting for this object so it's safe to
                # decref it.  If we are still waiting for it then we'll get
                # a callback later and we'll spot that it's no longer needed
                # at that point.
                _log.debug("Decreffing object %s", obj_id)
                self._ref_mgr.decref(obj_id, async=True)

    def discard_all(self):
        """
        Discards all references.
        """
        for obj_id in list(self.required_refs):
            self.discard_ref(obj_id)

    @actor_event
    def on_ref_acquired(self, obj_id, obj):
        was_ready = self.ready
        self.pending_increfs.discard(obj_id)
        if obj_id in self.required_refs:
            # Still required, record it.
            _log.debug("Reference %s acquired; still required", obj_id)
            self.acquired_refs[obj_id] = obj
        else:
            # Deleted while we were waiting.
            _log.debug("Object %s was discarded while waiting for its ref",
                       obj_id)
            self._ref_mgr.decref(obj_id)
        now_ready = self.ready
        if not was_ready and now_ready:
            _log.debug("Acquired all references, calling ready callback")
            self._ready_callback()

    def iteritems(self):
        """
        :returns: iterator over pairs of the currently valid references. Not
                  safe for concurrent modification of the set of IDs.
        """
        return self.acquired_refs.iteritems()

    @property
    def ready(self):
        return len(self.required_refs) == len(self.acquired_refs)

    def __getattr__(self, item):
        """
        Passes through getattr requests to the Actor to allow us to
        use @actor_event.
        """
        try:
            return super(RefHelper, self).__getattr__(item)
        except AttributeError:
            return getattr(self._actor, item)


class RefCountedActor(Actor):
    def __init__(self, qualifier=None):
        super(RefCountedActor, self).__init__(qualifier=qualifier)

        # These fields are owned by the ReferenceManager.
        self._manager = None
        self._id = None
        self.ref_mgmt_state = CREATED
        self.ref_count = 0

    def _notify_ready(self):
        """
        Utility method, to be called by subclass once its startup
        is complete.  Notifies the manager.
        """
        _log.debug("Notifying manager that %s is ready", self)
        gevent.spawn(self._manager.on_object_startup_complete,
                     self._id, self, async=True)

    def _notify_cleanup_complete(self):
        """
        Utility method, to be called by subclass once its cleanup
        is complete.  Notifies the manager.
        """
        _log.debug("Notifying manager that %s is done cleaning up", self)
        gevent.spawn(self._manager.on_object_cleanup_complete,
                     self._id, self, async=True)

    @actor_event
    def on_unreferenced(self):
        """
        Message sent by manager to tell this object to clean itself up
        for it can no longer be referenced.

        Must, eventually, result in a call to self._notify_cleanup_complete().

        This implementation immediately calls self._notify_cleanup_complete()
        """
        _log.debug("Default on_unreferenced() call, notifying cleanup done")
        self._notify_cleanup_complete()
