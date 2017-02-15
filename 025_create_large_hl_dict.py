from __future__ import division, print_function
import cPickle
import re
import time
import os
import numpy as np
import argparse

import LHCMeasurementTools.TimberManager as tm
import LHCMeasurementTools.LHC_Heatloads as hl
from LHCMeasurementTools.LHC_FBCT import FBCT
from LHCMeasurementTools.LHC_BCT import BCT
from LHCMeasurementTools.LHC_BQM import blength
from LHCMeasurementTools.SetOfHomogeneousVariables import SetOfHomogeneousNumericVariables
from LHCMeasurementTools.LHC_Energy import energy

import HeatLoadCalculators.impedance_heatload as hli
import HeatLoadCalculators.synchrotron_radiation_heatload as hls

import GasFlowHLCalculator.qbs_fill as qf

# Config
subtract_offset = True
average_offset_seconds = 600
hrs_after_sb = 24
hl_dict_dir = './hl_dicts/'

# Arguments
parser = argparse.ArgumentParser()
parser.add_argument('year', type=int)
parser.add_argument('-o', help='Force output filename', type=str)
parser.add_argument('--fills', help='Force fill list', nargs='+')
args = parser.parse_args()

if args.fills and not args.o:
    raise ValueError('If fills are specified, thet output has to, as well!')

# Find new version
if args.o:
    pkl_file_name = args.o
else:
    re_version = re.compile('^large_heat_load_dict_%i_(\d+).pkl$' % args.year)
    matches = filter(None, map(re_version.match, os.listdir(hl_dict_dir)))
    versions = map(lambda x: int(x.group(1)), matches)
    version = max(versions)+1

    pkl_file_name = hl_dict_dir + 'large_heat_load_dict_%i_%i.pkl' % (args.year, version)
    latest_pkl = hl_dict_dir + 'large_heat_load_dict_%i_latest.pkl' % args.year

logfile = pkl_file_name + '.log'

if args.year == 2016:
    base_folder = '/afs/cern.ch/work/l/lhcscrub/LHC_2016_25ns_beforeTS1/'
    child_folders = ['./']
    fills_bmodes_file = base_folder + '/fills_and_bmodes.pkl'
    csv_file_names = ['fill_basic_data_csvs/basic_data_fill_%d.csv',
            'fill_bunchbybunch_data_csvs/bunchbybunch_data_fill_%d.csv']
    filling_pattern_csv = base_folder + './fill_basic_data_csvs/injection_scheme.csv'
elif args.year == 2015:
    base_folder = '/afs/cern.ch/project/spsecloud/'
    child_folders = ['LHC_2015_PhysicsAfterTS2/', 'LHC_2015_PhysicsAfterTS3/', 'LHC_2015_Scrubbing50ns/', 'LHC_2015_IntRamp50ns/', 'LHC_2015_IntRamp25ns/']
    fills_bmodes_file = base_folder + child_folders[0] + 'fills_and_bmodes.pkl'
    csv_file_names = ['fill_csvs/fill_%d.csv']
    filling_pattern_csv = base_folder + child_folders[0] + 'injection_scheme_2015.csv'
else:
    raise ValueError('Invalid year!')

if os.path.isfile(pkl_file_name):
    raise ValueError('Pkl file already exists!')

# Filling pattern and bpi
re_bpi = re.compile('_(\d+)bpi')
filling_pattern_raw = tm.parse_timber_file(filling_pattern_csv, verbose=False)
key = filling_pattern_raw.keys()[0]
filling_pattern_ob = filling_pattern_raw[key]

# Arc correction factors
arc_correction_factor_list = hl.arc_average_correction_factors()
arcs_variable_list = hl.average_arcs_variable_list()
first_correct_filln = 4474 # from 016_
def correct_hl(heatloads):
    for factor, arc_variable in zip(arc_correction_factor_list, arcs_variable_list):
        heatloads.timber_variables[arc_variable].values *= factor

# Other functions
def add_to_dict(dictionary, value, keys, zero=False):
    if zero:
        value = 0
    for nn, key in enumerate(keys):
        if nn == len(keys)-1:
            if key not in dictionary:
                dictionary[key] = []
            dictionary[key].append(value)
        else:
            if key not in dictionary:
                dictionary[key] = {}
            dictionary = dictionary[key]

def cast_to_na_recursively(dictionary, assure_length=None):
    for key, item in dictionary.iteritems():
        if type(item) is dict:
            cast_to_na_recursively(item, assure_length)
        elif type(item) is list:
            dictionary[key] = np.array(item)
            if assure_length is not None and len(dictionary[key]) != assure_length:
                log_print('Expected length: %i, Actual length: %i for key %s' % (assure_length, len(dictionary[key]), key))
        else:
            log_print('Unexpected type in dictionary for key %s' % key)

def data_integration(timestamps, values, key):
    # Trapezoidal integration
    output = 0.
    nan = np.isnan(values)
    values[nan] = 0.
    #if np.sum(nan) > 0:
        #log_print('Fill %i: There have been nan values for var %s' % (filln,key))
    for i in xrange(len(values)-1):
        output += (timestamps[i+1] - timestamps[i])*(values[i] + values[i+1])/2.
    return output

def log_print(*args, **kwargs):
    with open(logfile, 'a') as f:
        print(*args, file=f, **kwargs)
    print(*args, **kwargs)
log_print('%s' % time.ctime())
log_print('Offset is subtracted?: %s' % subtract_offset)
log_print('Offset is the average of %i seconds before t_inj_proton' % average_offset_seconds)

# Time keys
time_key_list = ['t_start_fill', 't_inj_proton', 'start_ramp', 'stop_squeeze', 'stable_beams']
for ii in xrange(hrs_after_sb):
    time_key_list.append('sb+%i_hrs' % (ii+1))

# Time stamps
def get_time(kk):
    if kk == 0:
        tt = t_start_fill
    elif kk == 1:
        tt = t_start_injphys
    elif kk == 2:
        tt = t_start_ramp
    elif kk == 3:
        tt = t_stop_squeeze
    else:
        tt = t_stable_beams + (kk-4)*3600
    return tt

# Filling numbers
with open(fills_bmodes_file, 'r') as f:
    fills_and_bmodes = cPickle.load(f)
fills_0 = sorted(fills_and_bmodes.keys())

# Model heat load calculators
imp_calc = hli.HeatLoadCalculatorImpedanceLHCArc()
sr_calc = hls.HeatLoadCalculatorSynchrotronRadiationLHCArc()

# Main loop
output_dict = {}

if args.fills:
    fills_0 = sorted([int(x) for x in args.fills])
for filln in fills_0:
    process_fill = True

    # Check if this fill reached stable beams
    t_start_fill = fills_and_bmodes[filln]['t_startfill']
    t_start_injphys = fills_and_bmodes[filln]['t_start_INJPHYS']
    t_stable_beams = fills_and_bmodes[filln]['t_start_STABLE']
    if t_stable_beams == -1:
        log_print('Fill %i did not reach stable beams.' % filln)
        process_fill = False
    elif t_start_injphys == -1:
        log_print('Warning: Offset for fill %i could not be calculated as t_start_INJPROT is not in the fills and bmodes file!' % filln)
        process_fill = False

    # Check if all files exist and store their paths
    if process_fill:
        this_files = []
        for f in csv_file_names:
            f = f % filln
            for child in child_folders:
                path = base_folder + child + f
                if os.path.isfile(path):
                    this_files.append(path)
                    break
            else:
                log_print('Fill %i: %s does not exist' % (filln,f))
                process_fill = False
                break

    # Read csv and h5 files
    if process_fill:
        fill_dict = {}
        try:
            for f in this_files:
                if '.csv' in f:
                    fill_dict.update(tm.parse_timber_file(f, verbose=False))
                elif '.h5' in f:
                    fill_dict.update(tm.timber_variables_from_h5(f))
                else:
                    raise ValueError('Fill %i: Error: Unknown file type for %s.' % f)
        except IOError as e:
            log_print('Fill %i is skipped: %s!' % (filln,e))
            process_fill = False

    # Use recalculated data
    if process_fill:
        n_tries = 0
        while n_tries < 5:
            n_tries += 1
            try:
                qbs_ob = qf.compute_qbs_fill(filln)
                break
            except IOError as e:
                log_print('Fill %i: No recomputed data: %s!' % (filln,e))
                # Suspicious fails of read attempts -> try once more
                time.sleep(5)
        else:
            process_fill = False
            log_print('Fill %i: Recomputed data read attempt failed!' % filln)

        if process_fill:
            lhc_hl_dict = qf.lhc_arcs(qbs_ob)
            arc_averages = qf.compute_qbs_arc_avg(qbs_ob)

    # Main part - obtain and store the variables of interest
    if process_fill:
        log_print('Fill %i is being processed.' % filln)

        ## Allocate objects that are used later
        en_ob      = energy(fill_dict, beam=1)
        bct_bx     = {beam_n: BCT(fill_dict, beam=beam_n)     for beam_n in (1,2)}
        blength_bx = {beam_n: blength(fill_dict, beam=beam_n) for beam_n in (1,2)}
        fbct_bx    = {beam_n: FBCT(fill_dict, beam=beam_n)    for beam_n in (1,2)}

        ## Populate output dict

        # Fill Number
        add_to_dict(output_dict, filln, ['filln'])

        # Filling pattern and bpi
        pattern = filling_pattern_ob.nearest_older_sample(t_stable_beams)[0]
        add_to_dict(output_dict, pattern, ['filling_pattern'])
        bpi_info = re.search(re_bpi, pattern)
        if bpi_info is not None:
            bpi = int(bpi_info.group(1))
        else:
            bpi = -1
        add_to_dict(output_dict, bpi, ['bpi'])

        # Energy, only one per fill
        fill_energy = en_ob.nearest_older_sample(t_stable_beams)*1e9
        add_to_dict(output_dict, fill_energy, ['energy'])

        # subloop for time points
        t_start_ramp = fills_and_bmodes[filln]['t_start_RAMP']
        t_stop_squeeze = fills_and_bmodes[filln]['t_stop_SQUEEZE']
        end_time = fills_and_bmodes[filln]['t_endfill']

        for kk, time_key in enumerate(time_key_list):
            tt = get_time(kk)
            # zero controls if calculations for output are performed.
            # If zero is True, then only 0s are stored in the output_dict
            zero = tt > end_time
            this_add_to_dict = lambda x, keys: add_to_dict(output_dict, x, [time_key]+keys, zero=zero)

            # t_stamps
            this_add_to_dict(tt, ['t_stamps'])

            # intensity
            tot_int = 0
            int_bx = {}
            for beam in (1,2):
                if zero:
                    this_int = 0
                else:
                    this_int = float(bct_bx[beam].nearest_older_sample(tt))
                this_add_to_dict(this_int, ['intensity', 'b%i' % beam])
                int_bx[beam] = this_int
                tot_int += this_int
            this_add_to_dict(tot_int, ['intensity', 'total'])

            # Bunch length
            tot_avg, tot_var = 0, 0
            this_blength_bx = {}
            for beam in (1,2):
                if zero:
                    avg, sig = 0, 0
                else:
                    all_blen = blength_bx[beam].nearest_older_sample(tt)
                    mask_nonzero = all_blen != 0
                    if sum(mask_nonzero) == 0:
                        avg, sig = 0, 0
                    else:
                        avg = np.mean(all_blen[mask_nonzero])
                        sig = np.std(all_blen[mask_nonzero])
                this_blength_bx[beam] = avg
                this_add_to_dict(avg, ['blength', 'b%i' % beam, 'avg'])
                this_add_to_dict(sig, ['blength', 'b%i' % beam, 'sig'])
                tot_avg += avg
                tot_var += sig**2
            this_add_to_dict(tot_avg, ['blength', 'total', 'avg'])
            this_add_to_dict(np.sqrt(0.5*tot_var), ['blength', 'total', 'sig'])

            # Number of bunches
            n_bunches_bx = {}
            for beam in (1,2):
                if zero:
                    n_bunches = 0
                else:
                    bint = fbct_bx[beam].nearest_older_sample(tt)
                    min_int = 0.1 * max(bint)
                    mask_filled = bint > min_int
                n_bunches = sum(mask_filled)
                n_bunches_bx[beam] = n_bunches
                this_add_to_dict(n_bunches, ['n_bunches', 'b%i' % beam])

            # Imp / SR
            tot_imp, tot_sr = 0, 0
            for beam in (1,2):
                beam_int = int_bx[beam]
                n_bunches = n_bunches_bx[beam]
                sigma_t = this_blength_bx[beam]/4.
                if n_bunches != 0 and sigma_t != 0 and not zero:
                    imp = imp_calc.calculate_P_Wm(beam_int/n_bunches, sigma_t, fill_energy, n_bunches)
                    sr = sr_calc.calculate_P_Wm(beam_int/n_bunches, sigma_t, fill_energy, n_bunches)
                else:
                    imp, sr = 0, 0
                tot_imp += imp
                this_add_to_dict(imp, ['heat_load', 'imp', 'b%i' % beam])
                tot_sr += sr
                this_add_to_dict(sr, ['heat_load', 'sr', 'b%i' % beam])
            this_add_to_dict(tot_imp, ['heat_load', 'imp', 'total'])
            this_add_to_dict(tot_sr, ['heat_load', 'sr', 'total'])
            this_add_to_dict(tot_imp+tot_sr, ['heat_load', 'total_model'])

            # Heat loads
            for obj, main_key in zip([qbs_ob, arc_averages], ['all_cells', 'arc_averages']):
                index = np.argmin(np.abs(obj.timestamps - tt))
                mask_offset = np.logical_and(obj.timestamps < t_start_injphys, obj.timestamps > t_start_injphys - average_offset_seconds)
                for key, arr in obj.dictionary.iteritems():
                    offset = np.mean(arr[mask_offset])
                    hl = arr[index] - offset
                    integrated_hl = data_integration(obj.timestamps, arr-offset, key)
                    this_add_to_dict(hl, ['heat_load', main_key, key])
                    this_add_to_dict(offset, ['hl_subtracted_offset', main_key, key])
                    this_add_to_dict(offset, ['hl_integrated', main_key, key])


n_fills = len(output_dict['filln'])
cast_to_na_recursively(output_dict, assure_length=n_fills)

# Dump this dict
with open(pkl_file_name, 'w') as f:
    cPickle.dump(output_dict, f, protocol=-1)

if not args.o:
    os.remove(latest_pkl)
    os.symlink(os.path.basename(pkl_file_name), latest_pkl)

log_print('\nSuccess')
log_print('Saved to %s\n' % pkl_file_name)

