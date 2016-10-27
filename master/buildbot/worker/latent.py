# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Portions Copyright Buildbot Team Members
# Portions Copyright Canonical Ltd. 2009
from future.utils import itervalues

from twisted.internet import defer
from twisted.python import failure
from twisted.python import log
from zope.interface import implementer

from buildbot.interfaces import ILatentWorker
from buildbot.interfaces import LatentWorkerFailedToSubstantiate
from buildbot.interfaces import LatentWorkerSubstantiatiationCancelled
from buildbot.util import Notifier
from buildbot.worker.base import AbstractWorker


@implementer(ILatentWorker)
class AbstractLatentWorker(AbstractWorker):

    """A worker that will start up a worker instance when needed.

    To use, subclass and implement start_instance and stop_instance.

    See ec2.py for a concrete example.
    """

    substantiation_build = None
    insubstantiating = False
    build_wait_timer = None

    def checkConfig(self, name, password,
                    build_wait_timeout=60 * 10,
                    **kwargs):
        AbstractWorker.checkConfig(self, name, password, **kwargs)

    def reconfigService(self, name, password,
                        build_wait_timeout=60 * 10,
                        **kwargs):
        self._substantiation_notifier = Notifier()
        self.build_wait_timeout = build_wait_timeout
        return AbstractWorker.reconfigService(self, name, password, **kwargs)

    @property
    def building(self):
        # A LatentWorkerForBuilder will only be busy if it is building.
        return {wfb for wfb in itervalues(self.workerforbuilders)
                if wfb.isBusy()}

    def failed_to_start(self, instance_id, instance_state):
        log.msg('%s %s failed to start instance %s (%s)' %
                (self.__class__.__name__, self.workername,
                    instance_id, instance_state))
        raise LatentWorkerFailedToSubstantiate(instance_id, instance_state)

    def start_instance(self, build):
        # responsible for starting instance that will try to connect with this
        # master.  Should return deferred with either True (instance started)
        # or False (instance not started, so don't run a build here).  Problems
        # should use an errback.
        raise NotImplementedError

    def stop_instance(self, fast=False):
        # responsible for shutting down instance.
        raise NotImplementedError

    @property
    def substantiated(self):
        return self.conn is not None

    def substantiate(self, sb, build):
        if self.conn is not None:
            self._clearBuildWaitTimer()
            self._setBuildWaitTimer()
            return defer.succeed(True)
        if not self._substantiation_notifier:
            if self.parent and not self.missing_timer:
                # start timer.  if timer times out, fail deferred
                self.missing_timer = self.master.reactor.callLater(
                    self.missing_timeout,
                    self._substantiation_failed, defer.TimeoutError())
            self.substantiation_build = build
            # if substantiate fails synchronously we need to have the deferred
            # ready to be notified
            d = self._substantiation_notifier.wait()
            if self.conn is None:
                self._substantiate(build)
            # else: we're waiting for an old one to detach.  the _substantiate
            # will be done in ``detached`` below.
            return d
        return self._substantiation_notifier.wait()

    def _substantiate(self, build):
        # register event trigger
        try:
            d = self.start_instance(build)
        except Exception:
            # if start_instance crashes without defer, we still handle the
            # cleanup
            d = defer.fail(failure.Failure())

        def start_instance_result(result):
            # If we don't report success, then preparation failed.
            # we let the errback handle the issue
            if not result:
                # this behaviour is kept as compatibility, but it is better
                # to just errback with a workable reason
                msg = "Worker does not want to substantiate at this time"
                return failure.Failure(LatentWorkerFailedToSubstantiate(self.name, msg))
            return result

        def clean_up(failure):
            if self.missing_timer is not None:
                self.missing_timer.cancel()
            self._substantiation_failed(failure)
            # swallow the failure as it is given to notified
            return None
        d.addCallback(start_instance_result)
        d.addErrback(clean_up)
        return d

    @defer.inlineCallbacks
    def attached(self, bot):
        if not self._substantiation_notifier and self.build_wait_timeout >= 0:
            msg = 'Worker %s received connection while not trying to ' \
                'substantiate.  Disconnecting.' % (self.name,)
            log.msg(msg)
            self._disconnect(bot)
            raise RuntimeError(msg)

        try:
            yield AbstractWorker.attached(self, bot)
        except Exception:
            self._substantiation_failed(failure.Failure())
            return
        log.msg(r"Worker %s substantiated \o/" % (self.name,))

        if not self._substantiation_notifier:
            log.msg("No substantiation deferred for %s" % (self.name,))
        else:
            log.msg(
                "Firing %s substantiation deferred with success" % (self.name,))
            self.substantiation_build = None
            self._substantiation_notifier.notify(True)

    def attachBuilder(self, builder):
        sb = self.workerforbuilders.get(builder.name)
        return sb.attached(self, self.worker_commands)

    def detached(self):
        AbstractWorker.detached(self)
        if self._substantiation_notifier:
            d = self._substantiate(self.substantiation_build)
            d.addErrback(log.err, 'while re-substantiating')

    def _substantiation_failed(self, failure):
        self.missing_timer = None
        if self.substantiation_build:
            self.substantiation_build = None
            self._substantiation_notifier.notify(failure)
        d = self.insubstantiate()
        d.addErrback(log.err, 'while insubstantiating')
        # notify people, but only if we're still in the config
        if not self.parent or not self.notify_on_missing:
            return

        return self.master.data.updates.workerMissing(
            workerid=self.workerid,
            masterid=self.master.masterid,
            last_connection="Latent worker never connected",
            notify=self.notify_on_missing
        )

    def canStartBuild(self):
        # we were disconnected, but all the builds are not yet cleaned up.
        if self.conn is None and self.building:
            return False
        if self.insubstantiating:
            return False
        return AbstractWorker.canStartBuild(self)

    def buildStarted(self, sb):
        assert sb.isBusy()
        self._clearBuildWaitTimer()

    def buildFinished(self, sb):
        assert not sb.isBusy()
        if not self.building:
            if self.build_wait_timeout == 0:
                # we insubstantiate asynchronously to trigger more bugs with
                # the fake reactor
                self.master.reactor.callLater(0, self._soft_disconnect)
                # insubstantiate will automatically retry to create build for
                # this worker
            else:
                self._setBuildWaitTimer()

        # AbstractWorker.buildFinished() will try to start the next build for
        # that worker
        AbstractWorker.buildFinished(self, sb)

    def _clearBuildWaitTimer(self):
        if self.build_wait_timer is not None:
            if self.build_wait_timer.active():
                self.build_wait_timer.cancel()
            self.build_wait_timer = None

    def _setBuildWaitTimer(self):
        self._clearBuildWaitTimer()
        if self.build_wait_timeout <= 0:
            return
        self.build_wait_timer = self.master.reactor.callLater(
            self.build_wait_timeout, self._soft_disconnect)

    @defer.inlineCallbacks
    def insubstantiate(self, fast=False):
        self.insubstantiating = True
        self._clearBuildWaitTimer()
        d = self.stop_instance(fast)
        try:
            yield d
        except Exception as e:
            # The case of failure for insubstantiation is bad as we have a left-over costing ressource
            # There is not much thing to do here generically, so we must put the problem of stop_instance
            # reliability to the backend driver
            log.err(e, "while insubstantiating")

        self.insubstantiating = False
        if self._substantiation_notifier:
            self._substantiation_notifier.notify(
                failure.Failure(LatentWorkerSubstantiatiationCancelled()))
        self.botmaster.maybeStartBuildsForWorker(self.name)

    @defer.inlineCallbacks
    def _soft_disconnect(self, fast=False):
        if self.building:
            # wait until build finished
            return
        # a negative build_wait_timeout means the worker should never be shut
        # down, so just disconnect.
        if self.build_wait_timeout < 0:
            yield AbstractWorker.disconnect(self)
            return

        if self.missing_timer:
            self.missing_timer.cancel()
            self.missing_timer = None

        # if master is stopping, we will never achieve consistent state, as workermanager
        # wont accept new connection
        if self._substantiation_notifier and self.master.running:
            log.msg("Weird: Got request to stop before started. Allowing "
                    "worker to start cleanly to avoid inconsistent state")
            yield self._substantiation_notifier.wait()
            self.substantiation_build = None
            log.msg("Substantiation complete, immediately terminating.")

        if self.conn is not None:
            yield defer.DeferredList([
                AbstractWorker.disconnect(self),
                self.insubstantiate(fast)
            ], consumeErrors=True, fireOnOneErrback=True)
        else:
            yield AbstractWorker.disconnect(self)
            yield self.stop_instance(fast)

    def disconnect(self):
        # This returns a Deferred but we don't use it
        self._soft_disconnect()
        # this removes the worker from all builders.  It won't come back
        # without a restart (or maybe a sighup)
        self.botmaster.workerLost(self)

    @defer.inlineCallbacks
    def stopService(self):
        if self.conn is not None or self._substantiation_notifier:
            yield self._soft_disconnect()
        self._clearBuildWaitTimer()
        res = yield AbstractWorker.stopService(self)
        defer.returnValue(res)

    def updateWorker(self):
        """Called to add or remove builders after the worker has connected.

        Also called after botmaster's builders are initially set.

        @return: a Deferred that indicates when an attached worker has
        accepted the new builders and/or released the old ones."""
        for b in self.botmaster.getBuildersForWorker(self.name):
            if b.name not in self.workerforbuilders:
                b.addLatentWorker(self)
        return AbstractWorker.updateWorker(self)
