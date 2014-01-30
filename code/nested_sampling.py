"""
.. module:: nested_sampling
    :synopsis: Interface the MultiNest program with Monte Python

This implementation relies heavily on the existing Python wrapper for
MultiNest, called PyMultinest, written by Johannes Buchner, and available `at
this address <https://github.com/JohannesBuchner/PyMultiNest>`_ .

The main routine, :func:`run`, truly interfaces the two codes. It takes for
input the cosmological module, data and command line. It then defines
internally two functions, :func:`prior() <nested_sampling.prior>` and
:func:`loglike` that will serve as input for the run function of PyMultiNest.

.. moduleauthor:: Jesus Torrado <torradocacho@lorentz.leidenuniv.nl>
.. moduleauthor:: Benjamin Audren <benjamin.audren@epfl.ch>
"""
import pymultinest
import numpy as np
import os
import io_mp
import sampler
import warnings

def from_ns_output_to_chains_MULTIMODAL(data, command_line):
    """
    Translate the output of MultiNest into readable output for Monte Python

    This routine will be called after the MultiNest run has been successfully
    completed.

    If mode separation has been performed (i.e., multimodal=True), it creates
    'mode_#' subfolders containing a chain file with the corresponding samples
    and a 'log.param' file in which the starting point is the best fit of the
    nested sampling, and the same for the sigma. The minimum and maximum value
    are cropped to the extent of the modes in the case of the parameters used
    for the mode separation, and preserved in the rest.
    """
    # Open the 'stats.dat' file to see what happened and retrieve some info
    stats_name = data.ns_parameters['outputfiles_basename'] + 'stats.dat'
    stats_file = open(stats_name, 'r')
    lines = stats_file.readlines()
    stats_file.close()
    # Mode-separated info
    stats_mode_lines = {}
    i = 0
    for line in lines:
        if 'Nested Sampling Global Log-Evidence' in line:
            global_logZ, global_logZ_err = [float(a.strip()) for a in
                                            line.split(':')[1].split('+/-')]
        if 'Total Modes Found' in line:
            n_modes = int(line.split(':')[1].strip())
        if line[:4] == 'Mode':
            i += 1
            stats_mode_lines[i] = []
        if i:
            stats_mode_lines[i].append(line)
    assert n_modes == max(stats_mode_lines.keys()), (
        'Something is wrong... (strange error n.1)')

    # Prepare the accepted-points file -- modes are separated by 2 line breaks
    accepted_name = (data.ns_parameters['outputfiles_basename'] +
                    'post_separate.dat')
    with open(accepted_name, 'r') as accepted_file:
        mode_lines = [a for a in ''.join(accepted_file.readlines()).split('\n\n')
                      if a != '']
        assert len(mode_lines) == n_modes, 'Something is wrong... (strange error n.2)'
    accepted_chain_name = 'chain_NS__accepted.txt'
   
    # Preparing log.param files of modes
    with open(os.path.join(command_line.folder, 'log.param'), 'r') as log_file:
        log_lines = log_file.readlines()
    # Number of the lines to be changed
    varying_param_names = data.get_mcmc_parameters(['varying'])
    param_lines = {}
    pre = 'data.parameters['
    pos = ']'
    for i, line in enumerate(log_lines):
        if pre in line:
            if line.strip()[0] == '#':
                continue
            param_name = line.split('=')[0][line.find(pre)+len(pre):line.find(pos)]
            param_name = param_name.replace('"','').replace("'",'').strip()
            if param_name in varying_param_names:
                param_lines[param_name] = i

    # Parameters to cut: clustering_params, if exists, otherwise varying_params
    cut_params = data.ns_parameters.get('n_clustering_params')
    if cut_params:
        cut_param_names = varying_param_names[:cut_params]
    else:
        cut_param_names = varying_param_names

    # TODO: prepare total and rejected chain

    # Process each mode:
    for i in range(n_modes):
        # Create subfolder
        mode_subfolder = 'mode_'+str(i+1).zfill(len(str(n_modes)))
        mode_subfolder = os.path.join(command_line.folder, mode_subfolder)
        if not os.path.exists(mode_subfolder):
            os.makedirs(mode_subfolder)
        # Add ACCEPTED points
        mode_data = np.array(mode_lines[i].split(), dtype='float64')
        columns = 2+data.ns_parameters['n_params']
        mode_data = mode_data.reshape([mode_data.shape[0]/columns, columns])
        # Rearrange: sample-prob | -2*loglik | params
        #       ---> sample-prob |   -loglik | params
        mode_data[:, 1] = mode_data[: ,1] / 2.
        np.savetxt(os.path.join(mode_subfolder, accepted_chain_name),
                   mode_data, fmt='%.6e')
        # Get the necessary info of the parameters:
        #  -- max_posterior (MAP), sigma  <---  stats.dat file
        for j, line in enumerate(stats_mode_lines[i+1]):
            if 'Sigma' in line:
                line_sigma = j+1
            if 'MAP' in line:
                line_MAP = j+2
        MAPs   = {}
        sigmas = {}
        for j, param in enumerate(varying_param_names):
            n, MAP = stats_mode_lines[i+1][line_MAP+j].split()
            assert int(n) == j+1,  'Something is wrong... (strange error n.3)'
            MAPs[param] = MAP
            n, mean, sigma = stats_mode_lines[i+1][line_sigma+j].split()
            assert int(n) == j+1,  'Something is wrong... (strange error n.4)'
            sigmas[param] = sigma
        #  -- minimum rectangle containing the mode (only clustering params)
        mins = {}
        maxs = {}
        for j, param in enumerate(varying_param_names):
            if param in cut_param_names:
                mins[param] = min(mode_data[:, 2+j])
                maxs[param] = max(mode_data[:, 2+j])
            else:
                mins[param] = data.mcmc_parameters[param]['initial'][1]
                maxs[param] = data.mcmc_parameters[param]['initial'][2]
        # Create the log.param file
        for param in varying_param_names:
            line = pre+"'"+param+"'] = ["
            values = [MAPs[param], '%.6e'%mins[param], '%.6e'%maxs[param],
                      sigmas[param], '%e'%data.mcmc_parameters[param]['scale'],
                      "'"+data.mcmc_parameters[param]['role']+"'"]
            line += ', '.join(values) + ']\n'
            log_lines[param_lines[param]] = line

        # TODO: HANDLE SCALING!!!!

        with open(os.path.join(mode_subfolder, 'log.param'), 'w') as log_file:
            log_file.writelines(log_lines)

        # TODO: USE POINTS FROM TOTAL AND REJECTED SAMPLE???

        # # Creating chain from rejected points, with some interpretation of the
        # # weight associated to each point arXiv:0809.3437 sec 3
        # with open(basename+'ev.dat', 'r') as input_file:
        #     output = open(rejected_chain, 'w')
        #     array = np.loadtxt(input_file)
        #     output_array = np.zeros((np.shape(array)[0], np.shape(array)[1]-1))
        #     output_array[:, 0] = np.exp(array[:, -3]+array[:, -2]-log_evidence)
        #     output_array[:, 0] *= np.sum(output_array[:, 0])*np.shape(array)[0]
        #     output_array[:, 1] = -array[:, -3]
        #     output_array[:, 2:] = array[:, :-3]
        #     np.savetxt(
        #         output, output_array,
        #         fmt=' '.join(['%.6e' for _ in
        #                       range(np.shape(output_array)[1])]))
        #     output.close()


def from_ns_output_to_chains(folder, basename):
    """
    Translate the output of MultiNest into readable output for Monte Python

    This routine will be called after the MultiNest run has been successfully
    completed.

    """
    # First, take care of post_equal_weights (accepted points)
    accepted_chain = os.path.join(folder,
                                  'chain_NS__accepted.txt')
    rejected_chain = os.path.join(folder,
                                  'chain_NS__rejected.txt')

    # creating chain of accepted points (straightforward reshuffling of
    # columns)
    with open(basename+'post_equal_weights.dat', 'r') as input_file:
        output_file = open(accepted_chain, 'w')
        array = np.loadtxt(input_file)
        output_array = np.ones((np.shape(array)[0], np.shape(array)[1]+1))
        output_array[:, 1] = -array[:, -1]
        output_array[:, 2:] = array[:, :-1]
        np.savetxt(
            output_file, output_array,
            fmt='%i '+' '.join(['%.6e' for _ in
                               range(np.shape(array)[1])]))
        output_file.close()

    # Extracting log evidence
    with open(basename+'stats.dat') as input_file:
        lines = [line for line in input_file if 'Global Log-Evidence' in line]
        if len(lines) > 1:
            lines = [line for line in lines if 'Importance' in line]
        log_evidence = float(lines[0].split(':')[1].split('+/-')[0])

    # Creating chain from rejected points, with some interpretation of the
    # weight associated to each point arXiv:0809.3437 sec 3
    with open(basename+'ev.dat', 'r') as input_file:
        output = open(rejected_chain, 'w')
        array = np.loadtxt(input_file)
        output_array = np.zeros((np.shape(array)[0], np.shape(array)[1]-1))
        output_array[:, 0] = np.exp(array[:, -3]+array[:, -2]-log_evidence)
        output_array[:, 0] *= np.sum(output_array[:, 0])*np.shape(array)[0]
        output_array[:, 1] = -array[:, -3]
        output_array[:, 2:] = array[:, :-3]
        np.savetxt(
            output, output_array,
            fmt=' '.join(['%.6e' for _ in
                         range(np.shape(output_array)[1])]))
        output.close()


def run(cosmo, data, command_line):
    """
    Main call to prepare the information for the MultiNest run.

    Note the unusual set-up here, with the two following functions, `prior` and
    `loglike` having their docstrings written in the encompassing function.
    This trick was necessary as MultiNest required these two functions to be
    defined with a given number of parameters, so we could not add `data`. By
    defining them inside the run function, this problem was by-passed.

    .. function:: prior

        Generate the prior function for MultiNest

        It should transform the input unit cube into the parameter cube. This
        function actually wraps the method :func:`map_from_unit_interval()
        <prior.Prior.map_from_unit_interval>` of the class :class:`Prior
        <prior.Prior>`.

        :Parameters:
            **cube** (`array`) - Contains the current point in unit parameter
                space that has been selected within the MultiNest part.
            **ndim** (`int`) - Number of varying parameters
            **nparams** (`int`) - Total number of parameters, including the
                derived ones (not used, so hidden in `*args`)


    .. function:: loglike

        Generate the Likelihood function for MultiNest

        :Parameters:
            **cube** (`array`) - Contains the current point in the correct
                parameter space after transformation from :func:`prior`.
            **ndim** (`int`) - Number of varying parameters
            **nparams** (`int`) - Total number of parameters, including the
                derived ones (not used, so hidden in `*args`)

    """
    # Convenience variables
    varying_param_names = data.get_mcmc_parameters(['varying'])
    derived_param_names = data.get_mcmc_parameters(['derived'])

    # Check that all the priors are flat and that all the parameters are bound
    if not(all(data.mcmc_parameters[name]['prior'].prior_type == 'flat'
               for name in varying_param_names)):
        raise io_mp.ConfigurationError(
            'Nested Sampling with MultiNest is only possible with flat ' +
            'priors. Sorry!')
    if not(all(data.mcmc_parameters[name]['prior'].is_bound()
               for name in varying_param_names)):
        raise io_mp.ConfigurationError(
            'Nested Sampling with MultiNest is only possible for bound ' +
            'parameters. Set reasonable bounds for them in the ".param"' +
            'file.')

    def prior(cube, ndim, *args):
        """
        Please see the encompassing function docstring

        """
        for i, name in zip(range(ndim), varying_param_names):
            cube[i] = data.mcmc_parameters[name]['prior']\
                .map_from_unit_interval(cube[i])

    def loglike(cube, ndim, *args):
        """
        Please see the encompassing function docstring

        """
        # Updates values: cube --> data
        for i, name in zip(range(ndim), varying_param_names):
            data.mcmc_parameters[name]['current'] = cube[i]
        # Propagate the information towards the cosmo arguments
        data.update_cosmo_arguments()
        lkl = sampler.compute_lkl(cosmo, data)
        for i, name in enumerate(derived_param_names):
            cube[ndim+i] = data.mcmc_parameters[name]['current']
        return lkl

    # If absent, create the sub-folder NS
    ns_subfolder = os.path.join(command_line.folder, 'NS/')
    if not os.path.exists(ns_subfolder):
        os.makedirs(ns_subfolder)

    basename = os.path.join(
        ns_subfolder,
        command_line.folder.split(os.path.sep)[-2]+'-')

    # Prepare arguments for PyMultiNest
    # -- Automatic parameters
    data.ns_parameters['n_dims'] = len(varying_param_names)
    data.ns_parameters['n_params'] = (len(varying_param_names) +
                                      len(derived_param_names))
    data.ns_parameters['verbose'] = True
    data.ns_parameters['outputfiles_basename'] = basename
    # -- User-defined parameters
    parameters = ['n_live_points', 'sampling_efficiency', 'evidence_tolerance',
                  'importance_nested_sampling', 'const_efficiency_mode',
                  'log_zero', 'max_iter', 'seed', 'n_iter_before_update',
                  'multimodal', 'n_clustering_params', 'max_modes',
                  'mode_tolerance']
    prefix = 'NS_option_'
    for param in parameters:
        value = getattr(command_line, prefix+param)
        if value != -1:
            data.ns_parameters[param] = value
        # else: don't define them -> use PyMultiNest default value

    # One caveat: If multi-modal sampling is requested, Importance NS is disabled
    try:
        if data.ns_parameters['multimodal']:
            data.ns_parameters['importance_nested_sampling'] = False
            warnings.warn('Multi-modal sampling has been requested, '+
                          'so Importance Nested Sampling has been disabled')
    except KeyError:
        pass        

    # Launch MultiNest, and recover the output code
    output = pymultinest.run(loglike, prior, **data.ns_parameters)
#    output = None

    # Assuming this worked, i.e. if output is `None`, translate the output
    # ev.txt into the same format as standard Monte Python chains for further
    # analysis.
    if output is None:
        if data.ns_parameters['multimodal']:
            from_ns_output_to_chains_MULTIMODAL(data, command_line)
        else:
            from_ns_output_to_chains(command_line, data, basename)
