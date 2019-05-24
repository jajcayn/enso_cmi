"""
Main script for computing CMI on single timeseries.
"""

import cPickle
from datetime import datetime
from multiprocessing import Process, Queue

import numpy as np
import pyclits.mutual_inf as mutual
import xarray as xr
from pyclits.geofield import DataField
from pyclits.surrogates import SurrogateField
from tqdm import tqdm

PERIODS_SPAN = [5, 96]  # unit is month, 96
NUM_SURROGATES = 100
WORKERS = 20
NUM_BINS_EQQ = 4
K_KNN = 64

NINO_INDEX_BOUNDS = {
    "NINO3.4": {"lat": slice(-5, 5), "lon": slice(190, 240)},
    "NINO3": {"lat": slice(-5, 5), "lon": slice(210, 270)},
    "NINO4": {"lat": slice(-5, 5), "lon": slice(160, 210)},
    "NINO1.2": {"lat": slice(-10, 0), "lon": slice(270, 280)}
}

# TO BE CHANGED
DATASET_PATH = 'new_data_may19/tas_Amon_MPI-ESM-HR_historical_all_in_one_1880_1999_1.nc'
DATASET_VARIABLE = 'tas'
FIRST_DATE = '1900-01-01'

SAVING_FILENAME = 'bins/tas_Amon_MPI-ESM-HR'


class ResultsContainer:
    """
    Simple class for results container.
    """
    RESULT_ORDER = ['ph_ph_mi', 'ph_amp_mi', 'ph_ph_caus', 'ph_amp_caus']

    @staticmethod
    def validate_single_result(result):
        """
        Validate single result.
        """
        assert isinstance(result, (list, tuple)), (
            'Wrong type: %s' % type(result))
        assert len(result) == 4, ('Wrong length: %s' % len(result))
        for single_result in result:
            assert isinstance(single_result, dict)
            assert len(list(single_result.keys())) == 2
            for key, value in single_result.iteritems():
                assert isinstance(value, np.ndarray)

    @classmethod
    def from_saved_file(cls, filename):
        """
        Load from saved file.
        """
        pass

    def __init__(self, results, surrogates=False):
        """
        :results: list of results, order: phase-phase MI, phase-amp MI,
            phase-phase causality, and phase-amp causality
        """
        if not surrogates:
            self.validate_single_result(results)
        else:
            print("Found %d surrogate results" % len(results))
            for surrogate_result in results:
                self.validate_single_result(surrogate_result)

        self.surrogates = surrogates
        self.results = results

    def save(self, filename):
        """
        Save results to file.
        """
        saving_dict = {}
        if not self.surrogates:
            for result_str, result in zip(self.RESULT_ORDER, self.results):
                for key, value in result.iteritems():
                    saving_dict[result_str + "_" + key] = value

        else:
            for single_result in self.results:
                for result_str, result in zip(self.RESULT_ORDER,
                                              single_result):
                    for key, value in result.iteritems():
                        full_key = result_str + "_" + key
                        # we already have something, so stack
                        if full_key in saving_dict:
                            saving_dict[full_key] = np.dstack(
                                [saving_dict[full_key], value])
                        else:
                            saving_dict[full_key] = value

        with open(filename, "wb") as f:
            cPickle.dump(saving_dict, f, protocol=cPickle.HIGHEST_PROTOCOL)
        print('Saving done to %s' % filename)


def prepare_dataset(dataset_path, nino_region=None, surrogates=True):
    """
    Prepare dataset into single timeseries.

    :dataset_path: path to the dataset, to be loaded with xarray
    :nino_region: which nino region to use, if None, will not select data
    :surrogates: whether surrogates will be computed
    """
    dataset = xr.open_dataset(DATASET_PATH)
    variable = dataset[DATASET_VARIABLE]
    assert variable.ndim == 3, (
        'Currently supports only geospatial lat-lon datasets')
    # cut NINO region
    if nino_region is not None:
        variable = variable.sel(**NINO_INDEX_BOUNDS[nino_region])
    variable.sel(time=slice(FIRST_DATE, None))
    # do spatial mean
    variable = variable.mean(dim=["lat", "lon"])
    assert variable.ndim == 1, "Now the dataset should be 1dimensional"

    # cast datasat to DataField
    timeseries = DataField(data=variable.values)
    timeseries.create_time_array(
        date_from=datetime.strptime(FIRST_DATE, "%Y-%m-%d"), sampling='m')

    # prepare for surrogate creation
    if surrogates:
        seasonality = list(timeseries.get_seasonality(detrend=False))
        surrogate_field = SurrogateField()
        surrogate_field.copy_field(timeseries)

        timeseries.return_seasonality(seasonality[0], seasonality[1], None)
    print("Data loaded with shape %s" % (timeseries.data.shape))

    return timeseries, surrogate_field, seasonality


def compute_causality(timeseries1, timeseries2, tau_max, algorithm,
                      dim_condition=1, eta=0, phase_diff=False):
    """
    Compute causality as mean between 1 and tau_max.
    """
    results_simple = []
    results_knn = []
    for tau in range(1, tau_max):
        x, y, z = mutual.get_time_series_condition(
            [timeseries1, timeseries2], tau=tau, dim_condition=dim_condition,
            eta=eta, phase_diff=phase_diff)

        results_simple.append(mutual.cond_mutual_information(
            x, y, z, algorithm=algorithm, bins=NUM_BINS_EQQ))
        results_knn.append(mutual.knn_cond_mutual_information(
            x, y, z, k=K_KNN, dualtree=True))

    return np.mean(np.array(results_simple)), np.mean(np.array(results_knn))


def compute_information_measures(field, scales):
    """
    Computes information measures on a grid defined by scales on a given grid.
    :field: field to compute with
    :scales: scales for a grid
    """
    # prepare output arrays
    shape = (scales.shape[0], scales.shape[0])
    phase_phase_coherence = {'eqq': np.zeros(shape), 'knn': np.zeros(shape)}
    phase_amp_mi = {'eqq': np.zeros(shape), 'knn': np.zeros(shape)}
    phase_phase_causality = {'eqq': np.zeros(shape), 'knn': np.zeros(shape)}
    phase_amp_causality = {'eqq': np.zeros(shape), 'knn': np.zeros(shape)}

    for i, scale_i in enumerate(scales):
        field.wavelet(period=scale_i, period_unit='m', cut=1)
        phase_i = field.phase.copy()

        for j, scale_j in enumerate(scales):
            field.wavelet(period=scale_j, period_unit='m', cut=1)
            phase_j = field.phase.copy()
            amp_j = field.amplitude.copy()

            # compute phase-phase coherence
            phase_phase_coherence['eqq'][i, j] = mutual.mutual_information(
                phase_i, phase_j, algorithm='EQQ2', bins=NUM_BINS_EQQ)
            phase_phase_coherence['knn'][i, j] = mutual.knn_mutual_information(
                phase_i, phase_j, k=K_KNN, dualtree=True)

            # compute phase-amplitude mutual information
            phase_amp_mi['eqq'][i, j] = mutual.mutual_information(
                phase_i, amp_j, algorithm='EQQ2', bins=NUM_BINS_EQQ)
            phase_amp_mi['knn'][i, j] = mutual.knn_mutual_information(
                phase_i, amp_j, k=K_KNN, dualtree=True)

            # compute phase-phase causality
            eqq, knn = compute_causality(
                phase_i, phase_j, tau_max=7, algorithm="EQQ2", dim_condition=1,
                eta=0, phase_diff=True)
            phase_phase_causality['eqq'][i, j] = eqq
            phase_phase_causality['knn'][i, j] = knn

            # compute phase-amplitude causality
            eqq, knn = compute_causality(
                phase_i, np.power(amp_j, 2), tau_max=7, algorithm='GCM',
                dim_condition=3, eta=np.int(scale_i / 4), phase_diff=False)
            phase_amp_causality['eqq'][i, j] = eqq
            phase_amp_causality['knn'][i, j] = knn

    return (phase_phase_coherence, phase_amp_mi, phase_phase_causality,
            phase_amp_causality)


def _process_surrogates(field, seasonality, scales, jobq, resq):
    """
    Processes surrogates while job queue is not empty and puts results into
    result queue.
    """
    mean, var, _ = seasonality
    while True:
        s = jobq.get()
        # poison pill
        if s is None:
            break
        field.construct_fourier_surrogates(algorithm='FT')
        field.add_seasonality(mean, var, None)
        field.center_surr()
        surrogate_result = compute_information_measures(field, scales)
        resq.put(surrogate_result)


def main():
    # load timeseries
    timeseries, surrogate_field, seasonality = prepare_dataset(
        DATASET_PATH, "NINO3.4")
    timeseries.center_data()
    # get scales for computation
    scales = np.arange(PERIODS_SPAN[0], PERIODS_SPAN[-1] + 1, 1)

    # compute for data
    print("Starting computing for data...")
    data_results = compute_information_measures(timeseries, scales)
    print("Data done!")
    data_results = ResultsContainer(results=data_results, surrogates=False)
    data_results.save(filename=SAVING_FILENAME + '_data.bin')

    # compute for surrogates
    print("Starting computing for %d surrogates using %d wokers" % (
        NUM_SURROGATES, WORKERS))
    surrogates_done = 0
    all_surrogates_results = []
    # prepare queues
    job_queue = Queue()
    result_queue = Queue()
    for _ in range(NUM_SURROGATES):
        job_queue.put(1)
    for _ in range(WORKERS):
        job_queue.put(None)

    workers = [Process(
        target=_process_surrogates, args=(surrogate_field, seasonality, scales,
                                          job_queue, result_queue))
               for i in range(WORKERS)]
    # start workers
    for worker in workers:
        worker.start()
    # fetch results
    progress_bar = tqdm(total=NUM_SURROGATES)
    while surrogates_done < NUM_SURROGATES:
        all_surrogates_results.append(result_queue.get())
        surrogates_done += 1
        progress_bar.update(1)
    for worker in workers:
        worker.join()
    print("Surrogates done.")
    surrogate_result = ResultsContainer(
        results=all_surrogates_results, surrogates=True)
    surrogate_result.save(filename=SAVING_FILENAME + '_surrogates.bin')


if __name__ == "__main__":
    main()
