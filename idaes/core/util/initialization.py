# -*- coding: UTF-8 -*-
##############################################################################
# Institute for the Design of Advanced Energy Systems Process Systems
# Engineering Framework (IDAES PSE Framework) Copyright (c) 2018-2019, by the
# software owners: The Regents of the University of California, through
# Lawrence Berkeley National Laboratory,  National Technology & Engineering
# Solutions of Sandia, LLC, Carnegie Mellon University, West Virginia
# University Research Corporation, et al. All rights reserved.
#
# Please see the files COPYRIGHT.txt and LICENSE.txt for full copyright and
# license information, respectively. Both files are also available online
# at the URL "https://github.com/IDAES/idaes-pse".
##############################################################################
"""
This module contains utility functions for initialization of IDAES models.
"""

from pyomo.environ import Block, Var, TerminationCondition, SolverFactory
from pyomo.network import Arc
from pyomo.dae import ContinuousSet

from idaes.core import FlowsheetBlock
from idaes.core.util.exceptions import ConfigurationError
from idaes.core.util.model_statistics import degrees_of_freedom
from idaes.core.util.dyn_utils import (get_activity_dict,
        deactivate_model_at, deactivate_constraints_unindexed_by,
        fix_vars_unindexed_by, get_derivatives_at, copy_values_at_time)
import idaes.logger as idaeslog

__author__ = "Andrew Lee, John Siirola, Robert Parker"


def fix_state_vars(blk, state_args={}):
    """
    Method for fixing state variables within StateBlocks. Method takes an
    optional argument of values to use when fixing variables.

    Args:
        blk : An IDAES StateBlock object in which to fix the state variables
        state_args : a dict containing values to use when fixing state
                variables. Keys must match with names used in the
                define_state_vars method, and indices of any variables must
                agree.

    Returns:
        A dict keyed by block index, state variable name (as defined by
        define_state_variables) and variable index indicating the fixed status
        of each variable before the fix_state_vars method was applied.
    """
    # For sanity, handle cases where state_args is None
    if state_args is None:
        state_args = {}

    flags = {}
    for k in blk.keys():
        for n, v in blk[k].define_state_vars().items():
            for i in v:
                flags[k, n, i] = v[i].is_fixed()

                # If not fixed, fix at either guess provided or current value
                if not v[i].is_fixed():
                    if n in state_args:
                        # Try to get initial guess from state_args
                        try:
                            if i is None:
                                val = state_args[n]
                            else:
                                val = state_args[n][i]
                        except KeyError:
                            raise ConfigurationError(
                                'Indexes in state_args did not agree with '
                                'those of state variable {}. Please ensure '
                                'that indexes for initial guesses are correct.'
                                .format(n))
                        v[i].fix(val)
                    else:
                        # No guess, try to use current value
                        if v[i].value is not None:
                            v[i].fix()
                        else:
                            # No initial value - raise Exception before this
                            # gets to a solver.
                            raise ConfigurationError(
                                'State variable {} does not have a value '
                                'assigned. This usually occurs when a Var '
                                'is not assigned an initial value when it is '
                                'created. Please ensure all variables have '
                                'valid values before fixing them.'
                                .format(v.name))

    return flags


def revert_state_vars(blk, flags):
    """
    Method to revert the fixed state of the state variables within an IDAES
    StateBlock based on a set of flags of the previous state.

    Args:
        blk : an IDAES StateBlock
        flags : a dict of bools indicating previous state with keys in the form
                (StateBlock index, state variable name (as defined by
                define_state_vars), var indices).

    Returns:
        None
    """
    for k in blk.keys():
        for n, v in blk[k].define_state_vars().items():
            for i in v:
                try:
                    if not flags[k, n, i]:
                        v[i].unfix()
                except KeyError:
                    raise ConfigurationError(
                        'Indices of flags proved do not match with indices of'
                        'the StateBlock. Please make sure you are using the '
                        'correct StateBlock.')


def propagate_state(stream, direction="forward"):
    """
    This method propagates values between Ports along Arcs. Values can be
    propagated in either direction using the direction argument.

    Args:
        stream : Arc object along which to propagate values
        direction: direction in which to propagate values. Default = 'forward'
                Valid value: 'forward', 'backward'.

    Returns:
        None
    """
    if not isinstance(stream, Arc):
        raise TypeError("Unexpected type of stream argument. Value must be "
                        "a Pyomo Arc.")

    if direction == "forward":
        value_source = stream.source
        value_dest = stream.destination
    elif direction == "backward":
        value_source = stream.destination
        value_dest = stream.source
    else:
        raise ValueError("Unexpected value for direction argument: ({}). "
                         "Value must be either 'forward' or 'backward'."
                         .format(direction))

    for v in value_source.vars:
        for i in value_source.vars[v]:
            if not isinstance(value_dest.vars[v], Var):
                raise TypeError("Port contains one or more members which are "
                                "not Vars. propogate_state works by assigning "
                                "to the value attribute, thus can only be "
                                "when Port members are Pyomo Vars.")
            if not value_dest.vars[v][i].fixed:
                value_dest.vars[v][i].value = value_source.vars[v][i].value


# HACK, courtesy of J. Siirola
def solve_indexed_blocks(solver, blocks, **kwds):
    """
    This method allows for solving of Indexed Block components as if they were
    a single Block. A temporary Block object is created which is populated with
    the contents of the objects in the blocks argument and then solved.

    Args:
        solver : a Pyomo solver object to use when solving the Indexed Block
        blocks : an object which inherits from Block, or a list of Blocks
        kwds : a dict of argumnets to be passed to the solver

    Returns:
        A Pyomo solver results object
    """
    # Check blocks argument, and convert to a list of Blocks
    if isinstance(blocks, Block):
        blocks = [blocks]

    try:
        # Create a temporary Block
        tmp = Block(concrete=True)

        nBlocks = len(blocks)

        # Iterate over indexed objects
        for i, b in enumerate(blocks):
            # Check that object is a Block
            if not isinstance(b, Block):
                raise TypeError("Trying to apply solve_indexed_blocks to "
                                "object containing non-Block objects")
            # Append components of BlockData to temporary Block
            try:
                tmp._decl["block_%s" % i] = i
                tmp._decl_order.append((b, i+1 if i < nBlocks-1 else None))
            except Exception:
                raise Exception("solve_indexed_blocks method failed adding "
                                "components to temporary block.")

        # Set ctypes on temporary Block
        tmp._ctypes[Block] = [0, nBlocks-1, nBlocks]

        # Solve temporary Block
        results = solver.solve(tmp, **kwds)

    finally:
        # Clean up temporary Block contents so they are not removed when Block
        # is garbage collected.
        tmp._decl = {}
        tmp._decl_order = []
        tmp._ctypes = {}

    # Return results
    return results

def initialize_by_time_element(fs, time, **kwargs):
    """
    Function to initialize Flowsheet fs element-by-element along 
    ContinuousSet time. Assumes sufficient initialization/correct degrees 
    of freedom such that the first finite element can be solved immediately 
    and each subsequent finite element can be solved by fixing differential
    and derivative variables at the initial time point of that finite element.

    Args:
        fs : Flowsheet to initialize
        time : Set whose elements will be solved for individually
        solver : Pyomo solver object initialized with user's desired options
        outlvl : IDAES logger outlvl
        ignore_dof : Bool. If True, checks for square problems will be skipped.

    Returns:
        None
    """
    if not isinstance(fs, FlowsheetBlock):
        raise TypeError('First arg must be a FlowsheetBlock')
    if not isinstance(time, ContinuousSet):
        raise TypeError('Second arg must be a ContinuousSet')

    if time.get_discretization_info() == {}:
        raise ValueError('ContinuousSet must be discretized')

    scheme = time.get_discretization_info()['scheme']
    fep_list = time.get_finite_elements()
    nfe = time.get_discretization_info()['nfe']

    if scheme == 'LAGRANGE-RADAU':
        ncp = time.get_discretization_info()['ncp']
    elif scheme == 'LAGRANGE-LEGENDRE':
        msg = 'Initialization does not support collocation with Legendre roots'
        raise NotImplementedError(msg)
    elif scheme == 'BACKWARD Difference':
        ncp = 1
    elif scheme == 'FORWARD Difference':
        ncp = 1
        msg = 'Forward initialization (explicit Euler) has not yet been implemented'
        raise NotImplementedError(msg)
    elif scheme == 'CENTRAL Difference':
        msg = 'Initialization does not support central finite difference'
        raise NotImplementedError(msg)
    else:
        msg = 'Unrecognized discretization scheme. '
        'Has the model been discretized along the provided ContinuousSet?'
        raise ValueError(msg)
    # Disallow Central/Legendre discretizations.
    # Neither of these seem to be square by default for multi-finite element
    # initial value problems.

    # Create logger objects
    outlvl = kwargs.pop('outlvl', idaeslog.NOTSET)
    init_log = idaeslog.getInitLogger(__name__, level=outlvl) 
    solver_log = idaeslog.getSolveLogger(__name__, level=outlvl)

    solver = kwargs.pop('solver', SolverFactory('ipopt'))

    ignore_dof = kwargs.pop('ignore_dof', False)

    if not ignore_dof:
        if degrees_of_freedom(fs) != 0:
            msg = ('Model has nonzero degrees of freedom. This was unexpected. '
                  'Use keyword arg igore_dof=True to skip this check.')
            init_log.error(msg)
            raise ValueError('Nonzero degrees of freedom.')

 
    # Get dict telling which constraints/blocks are already inactive:
    # dict: id(compdata) -> bool (is active?)
    was_originally_active = get_activity_dict(fs)

    # Deactivate flowsheet except at t0, solve to ensure consistency
    # of initial conditions.
    non_initial_time = [t for t in time]
    non_initial_time.remove(time.first())
    deactivated = deactivate_model_at(fs, time, non_initial_time, 
            outlvl=idaeslog.ERROR)

    init_log.info(
    'Model is inactive except at t=0. Solving for consistent initial conditions.')
    with idaeslog.solver_log(solver_log, level=idaeslog.DEBUG) as slc:
        results = solver.solve(fs, tee=slc.tee)
    if results.solver.termination_condition == TerminationCondition.optimal:
        init_log.info('Successfully solved for consistent initial conditions')
    else:
        init_log.error('Failed to solve for consistent initial conditions')
        raise ValueError('Solver failed in initialization')

    deactivated[time.first()] = deactivate_model_at(fs, time, time.first(),
                                        outlvl=idaeslog.ERROR)[time.first()]

    # Here, deactivate non-time-indexed components. Do this after solve
    # for initial conditions in case these were used specify initial conditions
    con_unindexed_by_time = deactivate_constraints_unindexed_by(fs, time)
    var_unindexed_by_time = fix_vars_unindexed_by(fs, time)

    # Now model is completely inactive

    # For each timestep, we need to
    # 1. Activate model at points we're solving for
    # 2. Fix initial conditions (differential variables at previous timestep) 
    #    of finite element
    # 3. Solve the (now) square system
    # 4. Revert the model to its prior state

    # This will make use of the following dictionaries mapping 
    # time points -> time derivatives and time-differential variables
    derivs_at_time = get_derivatives_at(fs, time, [t for t in time])
    dvars_at_time = {t: [d.parent_component().get_state_var()[d.index()]
                         for d in derivs_at_time[t]]
                         for t in time}

    # Perform a solve for 1 -> nfe; i is the index of the finite element
    init_log.info('Flowsheet has been deactivated. Beginning element-wise initialization')
    for i in range(1, nfe+1):
        t_prev = time[(i-1)*ncp+1]
        # Non-initial time points in the finite element:
        fe = [time[k] for k in range((i-1)*ncp+2, i*ncp+2)]

        init_log.info(f'Entering step {i}/{nfe} of initialization')

        # Activate components of model that were active in the presumably
        # square original system
        for t in fe:
            for comp in deactivated[t]:
                if was_originally_active[id(comp)]:
                    comp.activate()

        # Get lists of derivative and differential variables
        # at initial time point of finite element
        init_deriv_list = derivs_at_time[t_prev]
        init_dvar_list = dvars_at_time[t_prev]

        # Record original fixed status of each of these variables
        was_originally_fixed = {}
        for drv in init_deriv_list:
            was_originally_fixed[id(drv)] = drv.fixed
            # Cannot fix variables with value None.
            # Any variable with value None was not solved for
            # (either stale or not included in previous solve)
            # and we don't want to fix it.
            if not drv.value is None:
                drv.fix()
        for dv in init_dvar_list:
            was_originally_fixed[id(dv)] = dv.fixed
            if not drv.value is None:
                dv.fix()

        # Initialize finite element from its initial conditions
        for t in fe:
            copy_values_at_time(fs, fs, t, t_prev, copy_fixed=False,
                                outlvl=idaeslog.ERROR)

        # Log that we are solving finite element {i}
        init_log.info(f'Solving finite element {i}')

        if not ignore_dof:
            if degrees_of_freedom(fs) != 0:
                msg = ('Model has nonzero degrees of freedom. This was unexpected. '
                      'Use keyword arg igore_dof=True to skip this check.')
                init_log.error(msg)
                raise ValueError('Nonzero degrees of freedom')
        
        with idaeslog.solver_log(solver_log, level=idaeslog.DEBUG) as slc:
            results = solver.solve(fs, tee=slc.tee)
        if results.solver.termination_condition == TerminationCondition.optimal:
           init_log.info(f'Successfully solved finite element {i}')
        else:
           init_log.error(f'Failed to solve finite element {i}')
           raise ValueError('Failure in initialization solve')

        # Deactivate components that may have been activated
        for t in fe:
            for comp in deactivated[t]:
                comp.deactivate()

        # Unfix variables that have been fixed
        for drv in init_deriv_list:
            if not was_originally_fixed[id(drv)]:
                drv.unfix()
        for dv in init_dvar_list:
            if not was_originally_fixed[id(dv)]:
                dv.unfix()

        # Log that initialization step {i} has been finished
        init_log.info(f'Initialization step {i} complete')

    # Reactivate components of the model that were originally active
    for t in time:
        for comp in deactivated[t]:
            if was_originally_active[id(comp)]:
                comp.activate()

    for con in con_unindexed_by_time:
        con.activate()
    for var in var_unindexed_by_time:
        var.unfix()

    # Logger message that initialization is finished
    init_log.info('Initialization completed. Model has been reactivated')
