# coding=utf-8

#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import datetime
import time

from openstack.baremetal import configdrive as os_configdrive
from oslo_config import cfg
from oslo_log import log
from oslo_serialization import jsonutils
from oslo_service import loopingcall
from oslo_utils import excutils
from oslo_utils import timeutils
import six

from ironic.common import boot_devices
from ironic.common import exception
from ironic.common import faults
from ironic.common.i18n import _
from ironic.common import network
from ironic.common import nova
from ironic.common import states
from ironic.conductor import notification_utils as notify_utils
from ironic.conductor import task_manager
from ironic.objects import fields

LOG = log.getLogger(__name__)
CONF = cfg.CONF


@task_manager.require_exclusive_lock
def node_set_boot_device(task, device, persistent=False):
    """Set the boot device for a node.

    If the node that the boot device change is being requested for
    is in ADOPTING state, the boot device will not be set as that
    change could potentially result in the future running state of
    an adopted node being modified erroneously.

    :param task: a TaskManager instance.
    :param device: Boot device. Values are vendor-specific.
    :param persistent: Whether to set next-boot, or make the change
        permanent. Default: False.
    :raises: InvalidParameterValue if the validation of the
        ManagementInterface fails.

    """
    task.driver.management.validate(task)
    if task.node.provision_state != states.ADOPTING:
        task.driver.management.set_boot_device(task,
                                               device=device,
                                               persistent=persistent)


def node_get_boot_mode(task):
    """Read currently set boot mode from a node.

    Reads the boot mode for a node. If boot mode can't be discovered,
    `None` is returned.

    :param task: a TaskManager instance.
    :raises: DriverOperationError or its derivative in case
             of driver runtime error.
    :raises: UnsupportedDriverExtension if current driver does not have
             management interface or `get_boot_mode()` method is
             not supported.
    :returns: Boot mode. One of :mod:`ironic.common.boot_mode` or `None`
        if boot mode can't be discovered
    """
    task.driver.management.validate(task)
    return task.driver.management.get_boot_mode(task)


# TODO(ietingof): remove `Sets the boot mode...` from the docstring
# once classic drivers are gone
@task_manager.require_exclusive_lock
def node_set_boot_mode(task, mode):
    """Set the boot mode for a node.

    Sets the boot mode for a node if the node's driver interface
    contains a 'management' interface.

    If the node that the boot mode change is being requested for
    is in ADOPTING state, the boot mode will not be set as that
    change could potentially result in the future running state of
    an adopted node being modified erroneously.

    :param task: a TaskManager instance.
    :param mode: Boot mode. Values are one of
        :mod:`ironic.common.boot_modes`
    :raises: InvalidParameterValue if the validation of the
             ManagementInterface fails.
    :raises: DriverOperationError or its derivative in case
             of driver runtime error.
    :raises: UnsupportedDriverExtension if current driver does not have
             vendor interface or method is unsupported.
    """
    if task.node.provision_state == states.ADOPTING:
        return

    task.driver.management.validate(task)

    boot_modes = task.driver.management.get_supported_boot_modes(task)

    if mode not in boot_modes:
        msg = _("Unsupported boot mode %(mode)s specified for "
                "node %(node_id)s. Supported boot modes are: "
                "%(modes)s") % {'mode': mode,
                                'modes': ', '.join(boot_modes),
                                'node_id': task.node.uuid}
        raise exception.InvalidParameterValue(msg)

    task.driver.management.set_boot_mode(task, mode=mode)


def node_wait_for_power_state(task, new_state, timeout=None):
    """Wait for node to be in new power state.

    :param task: a TaskManager instance.
    :param new_state: the desired new power state, one of the power states
        in :mod:`ironic.common.states`.
    :param timeout: number of seconds to wait before giving up. If not
        specified, uses the conductor.power_state_change_timeout config value.
    :raises: PowerStateFailure if timed out
    """
    retry_timeout = (timeout or CONF.conductor.power_state_change_timeout)

    def _wait():
        status = task.driver.power.get_power_state(task)
        if status == new_state:
            raise loopingcall.LoopingCallDone(retvalue=status)
        # NOTE(sambetts): Return False to trigger BackOffLoopingCall to start
        # backing off.
        return False

    try:
        timer = loopingcall.BackOffLoopingCall(_wait)
        return timer.start(initial_delay=1, timeout=retry_timeout).wait()
    except loopingcall.LoopingCallTimeOut:
        LOG.error('Timed out after %(retry_timeout)s secs waiting for '
                  '%(state)s on node %(node_id)s.',
                  {'retry_timeout': retry_timeout,
                   'state': new_state, 'node_id': task.node.uuid})
        raise exception.PowerStateFailure(pstate=new_state)


def _calculate_target_state(new_state):
    if new_state in (states.POWER_ON, states.REBOOT, states.SOFT_REBOOT):
        target_state = states.POWER_ON
    elif new_state in (states.POWER_OFF, states.SOFT_POWER_OFF):
        target_state = states.POWER_OFF
    else:
        target_state = None
    return target_state


def _can_skip_state_change(task, new_state):
    """Check if we can ignore the power state change request for the node.

    Check if we should ignore the requested power state change. This can occur
    if the requested power state is already the same as our current state. This
    only works for power on and power off state changes. More complex power
    state changes, like reboot, are not skipped.

    :param task: a TaskManager instance containing the node to act on.
    :param new_state: The requested power state to change to. This can be any
                      power state from ironic.common.states.
    :returns: True if should ignore the requested power state change. False
              otherwise
    """
    # We only ignore certain state changes. So if the desired new_state is not
    # one of them, then we can return early and not do an un-needed
    # get_power_state() call
    if new_state not in (states.POWER_ON, states.POWER_OFF,
                         states.SOFT_POWER_OFF):
        return False

    node = task.node

    def _not_going_to_change():
        # Neither the ironic service nor the hardware has erred. The
        # node is, for some reason, already in the requested state,
        # though we don't know why. eg, perhaps the user previously
        # requested the node POWER_ON, the network delayed those IPMI
        # packets, and they are trying again -- but the node finally
        # responds to the first request, and so the second request
        # gets to this check and stops.
        # This isn't an error, so we'll clear last_error field
        # (from previous operation), log a warning, and return.
        node['last_error'] = None
        # NOTE(dtantsur): under rare conditions we can get out of sync here
        node['power_state'] = curr_state
        node['target_power_state'] = states.NOSTATE
        node.save()
        notify_utils.emit_power_set_notification(
            task, fields.NotificationLevel.INFO,
            fields.NotificationStatus.END, new_state)
        LOG.warning("Not going to change node %(node)s power state because "
                    "current state = requested state = '%(state)s'.",
                    {'node': node.uuid, 'state': curr_state})

    try:
        curr_state = task.driver.power.get_power_state(task)
    except Exception as e:
        with excutils.save_and_reraise_exception():
            node['last_error'] = _(
                "Failed to change power state to '%(target)s'. "
                "Error: %(error)s") % {'target': new_state, 'error': e}
            node['target_power_state'] = states.NOSTATE
            node.save()
            notify_utils.emit_power_set_notification(
                task, fields.NotificationLevel.ERROR,
                fields.NotificationStatus.ERROR, new_state)

    if curr_state == states.POWER_ON:
        if new_state == states.POWER_ON:
            _not_going_to_change()
            return True
    elif curr_state == states.POWER_OFF:
        if new_state in (states.POWER_OFF, states.SOFT_POWER_OFF):
            _not_going_to_change()
            return True
    else:
        # if curr_state == states.ERROR:
        # be optimistic and continue action
        LOG.warning("Driver returns ERROR power state for node %s.",
                    node.uuid)
    return False


@task_manager.require_exclusive_lock
def node_power_action(task, new_state, timeout=None):
    """Change power state or reset for a node.

    Perform the requested power action if the transition is required.

    :param task: a TaskManager instance containing the node to act on.
    :param new_state: Any power state from ironic.common.states.
    :param timeout: timeout (in seconds) positive integer (> 0) for any
      power state. ``None`` indicates to use default timeout.
    :raises: InvalidParameterValue when the wrong state is specified
             or the wrong driver info is specified.
    :raises: StorageError when a failure occurs updating the node's
             storage interface upon setting power on.
    :raises: other exceptions by the node's power driver if something
             wrong occurred during the power action.

    """
    notify_utils.emit_power_set_notification(
        task, fields.NotificationLevel.INFO, fields.NotificationStatus.START,
        new_state)
    node = task.node

    if _can_skip_state_change(task, new_state):
        return
    target_state = _calculate_target_state(new_state)

    # Set the target_power_state and clear any last_error, if we're
    # starting a new operation. This will expose to other processes
    # and clients that work is in progress.
    if node['target_power_state'] != target_state:
        node['target_power_state'] = target_state
        node['last_error'] = None
        node.save()

    # take power action
    try:
        if (target_state == states.POWER_ON
                and node.provision_state == states.ACTIVE):
            task.driver.storage.attach_volumes(task)

        if new_state != states.REBOOT:
            task.driver.power.set_power_state(task, new_state, timeout=timeout)
        else:
            # TODO(TheJulia): We likely ought to consider toggling
            # volume attachments, although we have no mechanism to
            # really verify what cinder has connector wise.
            task.driver.power.reboot(task, timeout=timeout)
    except Exception as e:
        with excutils.save_and_reraise_exception():
            node['target_power_state'] = states.NOSTATE
            node['last_error'] = _(
                "Failed to change power state to '%(target_state)s' "
                "by '%(new_state)s'. Error: %(error)s") % {
                    'target_state': target_state,
                    'new_state': new_state,
                    'error': e}
            node.save()
            notify_utils.emit_power_set_notification(
                task, fields.NotificationLevel.ERROR,
                fields.NotificationStatus.ERROR, new_state)
    else:
        # success!
        node['power_state'] = target_state
        node['target_power_state'] = states.NOSTATE
        node.save()
        if node.instance_uuid:
            nova.power_update(
                task.context, node.instance_uuid, target_state)
        notify_utils.emit_power_set_notification(
            task, fields.NotificationLevel.INFO, fields.NotificationStatus.END,
            new_state)
        LOG.info('Successfully set node %(node)s power state to '
                 '%(target_state)s by %(new_state)s.',
                 {'node': node.uuid,
                  'target_state': target_state,
                  'new_state': new_state})
        # NOTE(TheJulia): Similarly to power-on, when we power-off
        # a node, we should detach any volume attachments.
        if (target_state == states.POWER_OFF
                and node.provision_state == states.ACTIVE):
            try:
                task.driver.storage.detach_volumes(task)
            except exception.StorageError as e:
                LOG.warning("Volume detachment for node %(node)s "
                            "failed. Error: %(error)s",
                            {'node': node.uuid, 'error': e})


@task_manager.require_exclusive_lock
def cleanup_after_timeout(task):
    """Cleanup deploy task after timeout.

    :param task: a TaskManager instance.
    """
    msg = (_('Timeout reached while waiting for callback for node %s')
           % task.node.uuid)
    deploying_error_handler(task, msg, msg)


def provisioning_error_handler(e, node, provision_state,
                               target_provision_state):
    """Set the node's provisioning states if error occurs.

    This hook gets called upon an exception being raised when spawning
    the worker to do some provisioning to a node like deployment, tear down,
    or cleaning.

    :param e: the exception object that was raised.
    :param node: an Ironic node object.
    :param provision_state: the provision state to be set on
        the node.
    :param target_provision_state: the target provision state to be
        set on the node.

    """
    if isinstance(e, exception.NoFreeConductorWorker):
        # NOTE(deva): there is no need to clear conductor_affinity
        #             because it isn't updated on a failed deploy
        node.provision_state = provision_state
        node.target_provision_state = target_provision_state
        node.last_error = (_("No free conductor workers available"))
        node.save()
        LOG.warning("No free conductor workers available to perform "
                    "an action on node %(node)s, setting node's "
                    "provision_state back to %(prov_state)s and "
                    "target_provision_state to %(tgt_prov_state)s.",
                    {'node': node.uuid, 'prov_state': provision_state,
                     'tgt_prov_state': target_provision_state})


def cleanup_cleanwait_timeout(task):
    """Cleanup a cleaning task after timeout.

    :param task: a TaskManager instance.
    """
    last_error = (_("Timeout reached while cleaning the node. Please "
                    "check if the ramdisk responsible for the cleaning is "
                    "running on the node. Failed on step %(step)s.") %
                  {'step': task.node.clean_step})
    # NOTE(rloo): this is called from the periodic task for cleanwait timeouts,
    # via the task manager's process_event(). The node has already been moved
    # to CLEANFAIL, so the error handler doesn't need to set the fail state.
    cleaning_error_handler(task, msg=last_error, set_fail_state=False)


def cleaning_error_handler(task, msg, tear_down_cleaning=True,
                           set_fail_state=True):
    """Put a failed node in CLEANFAIL and maintenance."""
    node = task.node
    node.fault = faults.CLEAN_FAILURE
    node.maintenance = True

    if tear_down_cleaning:
        try:
            task.driver.deploy.tear_down_cleaning(task)
        except Exception as e:
            msg2 = ('Failed to tear down cleaning on node %(uuid)s, '
                    'reason: %(err)s' % {'err': e, 'uuid': node.uuid})
            LOG.exception(msg2)
            msg = _('%s. Also failed to tear down cleaning.') % msg

    if node.provision_state in (
            states.CLEANING,
            states.CLEANWAIT,
            states.CLEANFAIL):
        # Clear clean step, msg should already include current step
        node.clean_step = {}
        info = node.driver_internal_info
        info.pop('clean_step_index', None)
        # Clear any leftover metadata about cleaning reboots
        info.pop('cleaning_reboot', None)
        node.driver_internal_info = info
    # For manual cleaning, the target provision state is MANAGEABLE, whereas
    # for automated cleaning, it is AVAILABLE.
    manual_clean = node.target_provision_state == states.MANAGEABLE
    node.last_error = msg
    node.maintenance_reason = msg
    node.save()

    if set_fail_state and node.provision_state != states.CLEANFAIL:
        target_state = states.MANAGEABLE if manual_clean else None
        task.process_event('fail', target_state=target_state)


def deploying_error_handler(task, logmsg, errmsg, traceback=False,
                            clean_up=True):
    """Put a failed node in DEPLOYFAIL.

    :param task: the task
    :param logmsg: message to be logged
    :param errmsg: message for the user
    :param traceback: Boolean; True to log a traceback
    :param clean_up: Boolean; True to clean up
    """
    node = task.node
    LOG.error(logmsg, exc_info=traceback)
    node.last_error = errmsg
    node.save()

    cleanup_err = None
    if clean_up:
        try:
            task.driver.deploy.clean_up(task)
        except Exception as e:
            msg = ('Cleanup failed for node %(node)s; reason: %(err)s'
                   % {'node': node.uuid, 'err': e})
            LOG.exception(msg)
            if isinstance(e, exception.IronicException):
                addl = _('Also failed to clean up due to: %s') % e
            else:
                addl = _('An unhandled exception was encountered while '
                         'aborting. More information may be found in the log '
                         'file.')
            cleanup_err = '%(err)s. %(add)s' % {'err': errmsg, 'add': addl}

    node.refresh()
    if node.provision_state in (
            states.DEPLOYING,
            states.DEPLOYWAIT,
            states.DEPLOYFAIL):
        # Clear deploy step; we leave the list of deploy steps
        # in node.driver_internal_info for debugging purposes.
        node.deploy_step = {}
        info = node.driver_internal_info
        info.pop('deploy_step_index', None)
        # Clear any leftover metadata about deployment reboots
        info.pop('deployment_reboot', None)
        node.driver_internal_info = info

    if cleanup_err:
        node.last_error = cleanup_err
    node.save()

    # NOTE(deva): there is no need to clear conductor_affinity
    task.process_event('fail')


@task_manager.require_exclusive_lock
def abort_on_conductor_take_over(task):
    """Set node's state when a task was aborted due to conductor take over.

    :param task: a TaskManager instance.
    """
    msg = _('Operation was aborted due to conductor take over')
    # By this time the "fail" even was processed, so we cannot end up in
    # CLEANING or CLEAN WAIT, only in CLEAN FAIL.
    if task.node.provision_state == states.CLEANFAIL:
        cleaning_error_handler(task, msg, set_fail_state=False)
    else:
        # For aborted deployment (and potentially other operations), just set
        # the last_error accordingly.
        task.node.last_error = msg
        task.node.save()

    LOG.warning('Aborted the current operation on node %s due to '
                'conductor take over', task.node.uuid)


def rescuing_error_handler(task, msg, set_fail_state=True):
    """Cleanup rescue task after timeout or failure.

    :param task: a TaskManager instance.
    :param msg: a message to set into node's last_error field
    :param set_fail_state: a boolean flag to indicate if node needs to be
                           transitioned to a failed state. By default node
                           would be transitioned to a failed state.
    """
    node = task.node
    try:
        node_power_action(task, states.POWER_OFF)
        task.driver.rescue.clean_up(task)
        node.last_error = msg
    except exception.IronicException as e:
        node.last_error = (_('Rescue operation was unsuccessful, clean up '
                             'failed for node: %(error)s') % {'error': e})
        LOG.error(('Rescue operation was unsuccessful, clean up failed for '
                   'node %(node)s: %(error)s'),
                  {'node': node.uuid, 'error': e})
    except Exception as e:
        node.last_error = (_('Rescue failed, but an unhandled exception was '
                             'encountered while aborting: %(error)s') %
                           {'error': e})
        LOG.exception('Rescue failed for node %(node)s, an exception was '
                      'encountered while aborting.', {'node': node.uuid})
    finally:
        node.save()

    if set_fail_state:
        try:
            task.process_event('fail')
        except exception.InvalidState:
            node = task.node
            LOG.error('Internal error. Node %(node)s in provision state '
                      '"%(state)s" could not transition to a failed state.',
                      {'node': node.uuid, 'state': node.provision_state})


@task_manager.require_exclusive_lock
def cleanup_rescuewait_timeout(task):
    """Cleanup rescue task after timeout.

    :param task: a TaskManager instance.
    """
    msg = _('Timeout reached while waiting for rescue ramdisk callback '
            'for node')
    errmsg = msg + ' %(node)s'
    LOG.error(errmsg, {'node': task.node.uuid})
    rescuing_error_handler(task, msg, set_fail_state=False)


def _spawn_error_handler(e, node, operation):
    """Handle error while trying to spawn a process.

    Handle error while trying to spawn a process to perform an
    operation on a node.

    :param e: the exception object that was raised.
    :param node: an Ironic node object.
    :param operation: the operation being performed on the node.
    """
    if isinstance(e, exception.NoFreeConductorWorker):
        node.last_error = (_("No free conductor workers available"))
        node.save()
        LOG.warning("No free conductor workers available to perform "
                    "%(operation)s on node %(node)s",
                    {'operation': operation, 'node': node.uuid})


def spawn_cleaning_error_handler(e, node):
    """Handle spawning error for node cleaning."""
    _spawn_error_handler(e, node, states.CLEANING)


def spawn_deploying_error_handler(e, node):
    """Handle spawning error for node deploying."""
    _spawn_error_handler(e, node, states.DEPLOYING)


def spawn_rescue_error_handler(e, node):
    """Handle spawning error for node rescue."""
    if isinstance(e, exception.NoFreeConductorWorker):
        remove_node_rescue_password(node, save=False)
    _spawn_error_handler(e, node, states.RESCUE)


def power_state_error_handler(e, node, power_state):
    """Set the node's power states if error occurs.

    This hook gets called upon an exception being raised when spawning
    the worker thread to change the power state of a node.

    :param e: the exception object that was raised.
    :param node: an Ironic node object.
    :param power_state: the power state to set on the node.

    """
    # NOTE This error will not emit a power state change notification since
    # this is related to spawning the worker thread, not the power state change
    # itself.
    if isinstance(e, exception.NoFreeConductorWorker):
        node.power_state = power_state
        node.target_power_state = states.NOSTATE
        node.last_error = (_("No free conductor workers available"))
        node.save()
        LOG.warning("No free conductor workers available to perform "
                    "an action on node %(node)s, setting node's "
                    "power state back to %(power_state)s.",
                    {'node': node.uuid, 'power_state': power_state})


@task_manager.require_exclusive_lock
def validate_port_physnet(task, port_obj):
    """Validate the consistency of physical networks of ports in a portgroup.

    Validate the consistency of a port's physical network with other ports in
    the same portgroup.  All ports in a portgroup should have the same value
    (which may be None) for their physical_network field.

    During creation or update of a port in a portgroup we apply the
    following validation criteria:

    - If the portgroup has existing ports with different physical networks, we
      raise PortgroupPhysnetInconsistent. This shouldn't ever happen.
    - If the port has a physical network that is inconsistent with other
      ports in the portgroup, we raise exception.Conflict.

    If a port's physical network is None, this indicates that ironic's VIF
    attachment mapping algorithm should operate in a legacy (physical
    network unaware) mode for this port or portgroup. This allows existing
    ironic nodes to continue to function after an upgrade to a release
    including physical network support.

    :param task: a TaskManager instance
    :param port_obj: a port object to be validated.
    :raises: Conflict if the port is a member of a portgroup which is on a
             different physical network.
    :raises: PortgroupPhysnetInconsistent if the port's portgroup has
             ports which are not all assigned the same physical network.
    """
    if 'portgroup_id' not in port_obj or not port_obj.portgroup_id:
        return

    delta = port_obj.obj_what_changed()
    # We can skip this step if the port's portgroup membership or physical
    # network assignment is not being changed (during creation these will
    # appear changed).
    if not (delta & {'portgroup_id', 'physical_network'}):
        return

    # Determine the current physical network of the portgroup.
    pg_physnets = network.get_physnets_by_portgroup_id(task,
                                                       port_obj.portgroup_id,
                                                       exclude_port=port_obj)

    if not pg_physnets:
        return

    # Check that the port has the same physical network as any existing
    # member ports.
    pg_physnet = pg_physnets.pop()
    port_physnet = (port_obj.physical_network
                    if 'physical_network' in port_obj else None)
    if port_physnet != pg_physnet:
        portgroup = network.get_portgroup_by_id(task, port_obj.portgroup_id)
        msg = _("Port with physical network %(physnet)s cannot become a "
                "member of port group %(portgroup)s which has ports in "
                "physical network %(pg_physnet)s.")
        raise exception.Conflict(
            msg % {'portgroup': portgroup.uuid, 'physnet': port_physnet,
                   'pg_physnet': pg_physnet})


def remove_node_rescue_password(node, save=True):
    """Helper to remove rescue password from a node.

    Removes rescue password from node. It saves node by default.
    If node should not be saved, then caller needs to explicitly
    indicate it.

    :param node: an Ironic node object.
    :param save: Boolean; True (default) to save the node; False
                 otherwise.
    """
    instance_info = node.instance_info
    if 'rescue_password' in instance_info:
        del instance_info['rescue_password']
        node.instance_info = instance_info
        if save:
            node.save()


def validate_instance_info_traits(node):
    """Validate traits in instance_info.

    All traits in instance_info must also exist as node traits.

    :param node: an Ironic node object.
    :raises: InvalidParameterValue if the instance traits are badly formatted,
        or contain traits that are not set on the node.
    """

    def invalid():
        err = (_("Error parsing traits from Node %(node)s instance_info "
                 "field. A list of strings is expected.")
               % {"node": node.uuid})
        raise exception.InvalidParameterValue(err)

    if not node.instance_info.get('traits'):
        return
    instance_traits = node.instance_info['traits']
    if not isinstance(instance_traits, list):
        invalid()
    if not all(isinstance(t, six.string_types) for t in instance_traits):
        invalid()

    node_traits = node.traits.get_trait_names()
    missing = set(instance_traits) - set(node_traits)
    if missing:
        err = (_("Cannot specify instance traits that are not also set on the "
                 "node. Node %(node)s is missing traits %(traits)s") %
               {"node": node.uuid, "traits": ", ".join(missing)})
        raise exception.InvalidParameterValue(err)


def _notify_conductor_resume_operation(task, operation, method):
    """Notify the conductor to resume an operation.

    :param task: the task
    :param operation: the operation, a string
    :param method: The name of the RPC method, a string
    """
    LOG.debug('Sending RPC to conductor to resume %(op)s for node %(node)s',
              {'op': operation, 'node': task.node.uuid})
    from ironic.conductor import rpcapi
    uuid = task.node.uuid
    rpc = rpcapi.ConductorAPI()
    topic = rpc.get_topic_for(task.node)
    # Need to release the lock to let the conductor take it
    task.release_resources()
    getattr(rpc, method)(task.context, uuid, topic=topic)


def notify_conductor_resume_clean(task):
    _notify_conductor_resume_operation(task, 'cleaning', 'continue_node_clean')


def notify_conductor_resume_deploy(task):
    _notify_conductor_resume_operation(task, 'deploying',
                                       'continue_node_deploy')


def skip_automated_cleaning(node):
    """Checks if node cleaning needs to be skipped for an specific node.

    :param node: the node to consider
    """
    return not CONF.conductor.automated_clean and not node.automated_clean


def power_on_node_if_needed(task):
    """Powers on node if it is powered off and has a Smart NIC port

    :param task: A TaskManager object
    :returns: the previous power state or None if no changes were made
    :raises: exception.NetworkError if agent status didn't match the required
        status after max retry attempts.
    """
    if not task.driver.network.need_power_on(task):
        return

    previous_power_state = task.driver.power.get_power_state(task)
    if previous_power_state == states.POWER_OFF:
        node_set_boot_device(
            task, boot_devices.BIOS, persistent=False)
        node_power_action(task, states.POWER_ON)

        # local import is necessary to avoid circular import
        from ironic.common import neutron

        host_id = None
        for port in task.ports:
            if neutron.is_smartnic_port(port):
                link_info = port.local_link_connection
                host_id = link_info['hostname']
                break

        if host_id:
            LOG.debug('Waiting for host %(host)s agent to be down',
                      {'host': host_id})

            client = neutron.get_client(context=task.context)
            neutron.wait_for_host_agent(
                client, host_id, target_state='down')
        return previous_power_state


def restore_power_state_if_needed(task, power_state_to_restore):
    """Change the node's power state if power_state_to_restore is not None

    :param task: A TaskManager object
    :param power_state_to_restore: power state
    """
    if power_state_to_restore:

        # Sleep is required here in order to give neutron agent
        # a chance to apply the changes before powering off.
        # Using twice the polling interval of the agent
        # "CONF.AGENT.polling_interval" would give the agent
        # enough time to apply network changes.
        time.sleep(CONF.agent.neutron_agent_poll_interval * 2)
        node_power_action(task, power_state_to_restore)


def build_configdrive(node, configdrive):
    """Build a configdrive from provided meta_data, network_data and user_data.

    If uuid or name are not provided in the meta_data, they're defauled to the
    node's uuid and name accordingly.

    :param node: an Ironic node object.
    :param configdrive: A configdrive as a dict with keys ``meta_data``,
        ``network_data`` and ``user_data`` (all optional).
    :returns: A gzipped and base64 encoded configdrive as a string.
    """
    meta_data = configdrive.setdefault('meta_data', {})
    meta_data.setdefault('uuid', node.uuid)
    if node.name:
        meta_data.setdefault('name', node.name)

    user_data = configdrive.get('user_data')
    if isinstance(user_data, (dict, list)):
        user_data = jsonutils.dump_as_bytes(user_data)
    elif user_data:
        user_data = user_data.encode('utf-8')

    LOG.debug('Building a configdrive for node %s', node.uuid)
    return os_configdrive.build(meta_data, user_data=user_data,
                                network_data=configdrive.get('network_data'))


def fast_track_able(task):
    """Checks if the operation can be a streamlined deployment sequence.

    This is mainly focused on ensuring that we are able to quickly sequence
    through operations if we already have a ramdisk heartbeating through
    external means.

    :param task: Taskmanager object
    :returns: True if [deploy]fast_track is set to True, no iSCSI boot
              configuration is present, and no last_error is present for
              the node indicating that there was a recent failure.
    """
    return (CONF.deploy.fast_track
            # TODO(TheJulia): Network model aside, we should be able to
            # fast-track through initial sequence to complete deployment.
            # This needs to be validated.
            # TODO(TheJulia): Do we need a secondary guard? To prevent
            # driving through this we could query the API endpoint of
            # the agent with a short timeout such as 10 seconds, which
            # would help verify if the node is online.
            # TODO(TheJulia): Should we check the provisioning/deployment
            # networks match config wise? Do we care? #decisionsdecisions
            and task.driver.storage.should_write_image(task)
            and task.node.last_error is None)


def is_fast_track(task):
    """Checks a fast track is available.

    This method first ensures that the node and conductor configuration
    is valid to perform a fast track sequence meaning that we already
    have a ramdisk running through another means like discovery.
    If not valid, False is returned.

    The method then checks for the last agent heartbeat, and if it occured
    within the timeout set by [deploy]fast_track_timeout and the power
    state for the machine is POWER_ON, then fast track is permitted.

    :param task: Taskmanager object
    :returns: True if the last heartbeat that was recorded was within
              the [deploy]fast_track_timeout setting.
    """
    if not fast_track_able(task):
        return False
    # use native datetime objects for conversion and compare
    # slightly odd because py2 compatability :(
    last = datetime.datetime.strptime(
        task.node.driver_internal_info.get(
            'agent_last_heartbeat',
            '1970-01-01T00:00:00.000000'),
        "%Y-%m-%dT%H:%M:%S.%f")
    # If we found nothing, we assume that the time is essentially epoch.
    time_delta = datetime.timedelta(seconds=CONF.deploy.fast_track_timeout)
    last_valid = timeutils.utcnow() - time_delta
    # Checking the power state, because if we find the machine off due to
    # any action, we can't actually fast track the node. :(
    return (last_valid <= last
            and task.driver.power.get_power_state(task) == states.POWER_ON)
