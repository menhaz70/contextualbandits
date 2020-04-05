import numpy as np, types, warnings, multiprocessing
from copy import deepcopy
from joblib import Parallel, delayed
import pandas as pd
import ctypes
from scipy.stats import norm as norm_dist
from scipy.sparse import issparse, isspmatrix_csr, csr_matrix
from sklearn.linear_model import LogisticRegression
from .linreg import LinearRegression, _wrapper_double
from ._cy_utils import _matrix_inv_symm

_unexpected_err_msg = "Unexpected error. Please open an issue in GitHub describing what you were doing."

def _convert_decision_function_w_sigmoid(classifier):
    if 'decision_function' in dir(classifier):
        classifier.decision_function_w_sigmoid = types.MethodType(_decision_function_w_sigmoid, classifier)
        #### Note: the weird name is to avoid potential collisions with user-defined methods
    elif 'predict' in dir(classifier):
        classifier.decision_function_w_sigmoid = types.MethodType(_decision_function_w_sigmoid_from_predict, classifier)
    else:
        raise ValueError("Classifier must have at least one of 'predict_proba', 'decision_function', 'predict'.")
    return classifier

def _add_method_predict_robust(classifier):
    if 'predict_proba' in dir(classifier):
        classifier.predict_proba_robust = types.MethodType(_robust_predict_proba, classifier)
    if 'decision_function_w_sigmoid' in dir(classifier):
        classifier.decision_function_robust = types.MethodType(_robust_decision_function_w_sigmoid, classifier)
    elif 'decision_function' in dir(classifier):
        classifier.decision_function_robust = types.MethodType(_robust_decision_function, classifier)
    if 'predict' in dir(classifier):
        classifier.predict_robust = types.MethodType(_robust_predict, classifier)

    return classifier

def _robust_predict(self, X):
    try:
        return self.predict(X).reshape(-1)
    except:
        return np.zeros(X.shape[0])

def _robust_predict_proba(self, X):
    try:
        return self.predict_proba(X)
    except:
        return np.zeros((X.shape[0], 2))

def _robust_decision_function(self, X):
    try:
        return self.decision_function(X).reshape(-1)
    except:
        return np.zeros(X.shape[0])

def _robust_decision_function_w_sigmoid(self, X):
    try:
        return self.decision_function_w_sigmoid(X).reshape(-1)
    except:
        return np.zeros(X.shape[0])

def _decision_function_w_sigmoid(self, X):
    pred = self.decision_function(X).reshape(-1)
    _apply_sigmoid(pred)
    return pred

def _decision_function_w_sigmoid_from_predict(self, X):
    return self.predict(X).reshape(-1)

def _check_bools(batch_train=False, assume_unique_reward=False):
    return bool(batch_train), bool(assume_unique_reward)

def _check_refit_buffer(refit_buffer, batch_train):
    if not batch_train:
        refit_buffer = None
    if refit_buffer == 0:
        refit_buffer = None
    if refit_buffer is not None:
        assert refit_buffer > 0
        if isinstance(refit_buffer, float):
            refit_buffer = int(refit_buffer)
    return refit_buffer

def _check_random_state(random_state):
    if random_state is None:
        return np.random.Generator(np.random.MT19937())
    if isinstance(random_state, np.random.Generator):
        return random_state
    elif isinstance(random_state, np.random.RandomState) or (random_state == np.random):
        random_state = int(random_state.randint(np.iinfo(np.int32).max) + 1)
    if isinstance(random_state, float):
        random_state = int(random_state)
    assert random_state > 0
    return np.random.Generator(np.random.MT19937(seed = random_state))

def _check_constructor_input(base_algorithm, nchoices, batch_train=False):
    if isinstance(base_algorithm, list):
        if len(base_algorithm) != nchoices:
            raise ValueError("Number of classifiers does not match with number of choices.")
        ### For speed reasons, here it will not test if each classifier has the right methods
    else:
        assert ('fit' in dir(base_algorithm))
        assert ('predict_proba' in dir(base_algorithm)) or ('decision_function' in dir(base_algorithm)) or ('predict' in dir(base_algorithm))
        if batch_train:
            assert 'partial_fit' in dir(base_algorithm)

    assert nchoices >= 2
    assert isinstance(nchoices, int)

def _check_njobs(njobs):
    if njobs < 1:
        njobs = multiprocessing.cpu_count()
    if njobs is None:
        return 1
    assert isinstance(njobs, int)
    assert njobs >= 1
    return njobs


def _check_beta_prior(beta_prior, nchoices, for_ucb=False):
    if beta_prior == 'auto':
        if not for_ucb:
            out = ( (3.0 / float(nchoices), 4.0), 2 )
        else:
            out = ( (5.0 / float(nchoices), 4.0), 2 )
    elif beta_prior is None:
        out = ((1.0,1.0), 0)
    else:
        assert len(beta_prior) == 2
        assert len(beta_prior[0]) == 2
        assert isinstance(beta_prior[1], int)
        assert isinstance(beta_prior[0][0], int) or isinstance(beta_prior[0][0], float)
        assert isinstance(beta_prior[0][1], int) or isinstance(beta_prior[0][1], float)
        assert (beta_prior[0][0] > 0.) and (beta_prior[0][1] > 0.)
        out = beta_prior
    return out

def _check_smoothing(smoothing):
    if smoothing is None:
        return None
    assert len(smoothing) >= 2
    assert (smoothing[0] >= 0) & (smoothing[1] >= 0)
    assert smoothing[1] > smoothing[0]
    return float(smoothing[0]), float(smoothing[1])


def _check_fit_input(X, a, r, choice_names = None):
    X = _check_X_input(X)
    a = _check_1d_inp(a)
    r = _check_1d_inp(r)
    assert X.shape[0] == a.shape[0]
    assert X.shape[0] == r.shape[0]
    if choice_names is not None:
        a = pd.Categorical(a, choice_names).codes
        if pd.isnull(a).sum() > 0:
            raise ValueError("Input contains actions/arms that this object does not have.")
    return X, a, r

def _check_X_input(X):
    if (X.__class__.__name__ == 'DataFrame') or isinstance(X, pd.core.frame.DataFrame):
        X = X.values
    if isinstance(X, np.matrixlib.defmatrix.matrix):
        warnings.warn("'defmatrix' will be cast to array.")
        X = np.array(X)
    if not isinstance(X, np.ndarray):
        raise ValueError("'X' must be a numpy array or pandas data frame.")
    if len(X.shape) == 1:
        X = X.reshape((1, -1))
    assert len(X.shape) == 2
    return X

def _check_1d_inp(y):
    if y.__class__.__name__ == 'DataFrame' or y.__class__.__name__ == 'Series':
        y = y.values
    if type(y) == np.matrixlib.defmatrix.matrix:
        warnings.warn("'defmatrix' will be cast to array.")
        y = np.array(y)
    if type(y) != np.ndarray:
        raise ValueError("'a' and 'r' must be numpy arrays or pandas data frames.")
    if len(y.shape) == 2:
        assert y.shape[1] == 1
        y = y.reshape(-1)
    assert len(y.shape) == 1
    return y

def _check_bay_inp(method, n_iter, n_samples):
    assert method in ['advi','nuts', 'metropolis']
    if n_iter == 'auto':
        if method == 'nuts':
            n_iter = 10000
        elif method == 'metropolis':
            n_iter = 25000
        else:
            n_iter = 5000
    assert n_iter > 0
    if isinstance(n_iter, float):
        n_iter = int(n_iter)
    assert isinstance(n_iter, int)

    assert n_samples > 0
    if isinstance(n_samples, float):
        n_samples = int(n_samples)
    assert isinstance(n_samples, int)

    return n_iter, n_samples

def _check_active_inp(self, base_algorithm, f_grad_norm, case_one_class):
    if f_grad_norm == 'auto':
        _check_autograd_supported(base_algorithm)
        self._get_grad_norms = _get_logistic_grads_norms
    else:
        assert callable(f_grad_norm)
        self._get_grad_norms = f_grad_norm

    if case_one_class == 'auto':
        self._force_fit = False
        self._rand_grad_norms = _gen_random_grad_norms
    elif case_one_class == 'zero':
        self._force_fit = False
        self._rand_grad_norms = _gen_zero_norms
    elif case_one_class is None:
        self._force_fit = True
        self._rand_grad_norms = None
    else:
        assert callable(case_one_class)
        self._force_fit = False
        self._rand_grad_norms = case_one_class
    self.case_one_class = case_one_class

def _check_refit_inp(refit_buffer_X, refit_buffer_r, refit_buffer):
    if (refit_buffer_X is not None) or (refit_buffer_y is not None):
        if not refit_buffer:
            msg  = "Can only pass 'refit_buffer_X' and 'refit_buffer_r' "
            msg += "when using 'refit_buffer'."
            raise ValueError(msg)
        if (refit_buffer_X is None) or (refit_buffer_y is None):
            msg  = "'refit_buffer_X' and 'refit_buffer_y "
            msg += "must be passed in conjunction."
            raise ValueError(msg)
        refit_buffer_X = _check_X_input(refit_buffer_X)
        refit_buffer_r = _check_1d_inp(refit_buffer_r)
        assert refit_buffer_X.shape[0] == refit_buffer_r.shape[0]
        if refit_buffer_X.shape[0] == 0:
            refit_buffer_X = None
            refit_buffer_r = None
    return refit_buffer_X, refit_buffer_r

def _extract_regularization(base_algorithm):
    if base_algorithm.__class__.__name__ == 'LogisticRegression':
        return 1.0 / base_algorithm.C
    elif base_algorithm.__class__.__name__ == 'SGDClassifier':
        return base_algorithm.alpha
    elif base_algorithm.__class__.__name__ == 'RidgeClassifier':
        return base_algorithm.alpha
    elif base_algorithm.__class__.__name__ == 'StochasticLogisticRegression':
        return base_algorithm.reg_param
    elif base_algorithm.__class__.__name__ == "LinearRegression":
        if not ("lambda_" in dir(base_algorithm)):
            return 0.
        return base_algorithm.lambda_
    else:
        msg  = "'auto' option only available for "
        msg += "'LogisticRegression', 'SGDClassifier', 'RidgeClassifier', "
        msg += "'StochasticLogisticRegression' (stochQN's), "
        msg += "and 'LinearRegression' (this package's only)."
        raise ValueError(msg)

def _logistic_grad_norm(X, y, pred, base_algorithm):
    coef = base_algorithm.coef_.reshape(-1)[:X.shape[1]]
    err = pred - y

    if issparse(X):
        if not isspmatrix_csr(X):
            warnings.warn("Sparse matrix will be cast to CSR format.")
            X = csr_matrix(X)
        grad_norm = X.multiply(err)
    else:
        grad_norm = X * err.reshape((-1, 1))

    ### Note: since this is done on a row-by-row basis on two classes only,
    ### it doesn't matter whether the loss function is summed or averaged over
    ### data points, or whether there is regularization or not.

    ## coefficients
    grad_norm = np.einsum("ij,ij->i", grad_norm, grad_norm)

    ## intercept
    if base_algorithm.fit_intercept:
        grad_norm += err ** 2

    return grad_norm

def _get_logistic_grads_norms(base_algorithm, X, pred):
    return np.c_[_logistic_grad_norm(X, 0, pred, base_algorithm), _logistic_grad_norm(X, 1, pred, base_algorithm)]

def _check_autograd_supported(base_algorithm):
    supported = ['LogisticRegression', 'SGDClassifier', 'RidgeClassifier', 'StochasticLogisticRegression', 'LinearRegression']
    if not base_algorithm.__class__.__name__ in supported:
        raise ValueError("Automatic gradients only implemented for the following classes: " + ", ".join(supported))
    if base_algorithm.__class__.__name__ == 'LogisticRegression':
        if base_algorithm.penalty != 'l2':
            raise ValueError("Automatic gradients only defined for LogisticRegression with l2 regularization.")
        if base_algorithm.intercept_scaling != 1:
            raise ValueError("Automatic gradients for LogisticRegression not implemented with 'intercept_scaling'.")

    if base_algorithm.__class__.__name__ == 'RidgeClassifier':
        if base_algorithm.normalize:
            raise ValueError("Automatic gradients for LogisticRegression only implemented without 'normalize'.")

    if base_algorithm.__class__.__name__ == 'SGDClassifier':
        if base_algorithm.loss != 'log':
            raise ValueError("Automatic gradients for LogisticRegression only implemented with logistic loss.")
        if base_algorithm.penalty != 'l2':
            raise ValueError("Automatic gradients only defined for LogisticRegression with l2 regularization.")
    
    try:
        if base_algorithm.class_weight is not None:
            raise ValueError("Automatic gradients for LogisticRegression not supported with 'class_weight'.")
    except:
        pass

def _gen_random_grad_norms(X, n_pos, n_neg, random_state):
    ### Note: there isn't any theoretical reason behind these chosen distributions and numbers.
    ### A custom function might do a lot better.
    magic_number = np.log10(X.shape[1])
    smooth_prop = (n_pos + 1.0) / (n_pos + n_neg + 2.0)
    return np.c_[random_state.gamma(magic_number / smooth_prop, magic_number, size=X.shape[0]),
                 random_state.gamma(magic_number * smooth_prop, magic_number, size=X.shape[0])]

def _gen_zero_norms(X, n_pos, n_neg):
    return np.zeros((X.shape[0], 2))

def _apply_smoothing(preds, smoothing, counts):
    if (smoothing is not None) and (counts is not None):
        preds[:, :] = (preds * counts + smoothing[0]) / (counts + smoothing[1])
    return None

def _apply_sigmoid(x):
    if (len(x.shape) == 2):
        x[:, :] = 1.0 / (1.0 + np.exp(-x))
    else:
        x[:] = 1.0 / (1.0 + np.exp(-x))
    return None

def _apply_inverse_sigmoid(x):
    x[x == 0] = 1e-8
    x[x == 1] = 1 - 1e-8
    if (len(x.shape) == 2):
        x[:, :] = np.log(x / (1.0 - x))
    else:
        x[:] = np.log(x / (1.0 - x))
    return None

def _apply_softmax(x):
    x[:, :] = np.exp(x - x.max(axis=1).reshape((-1, 1)))
    x[:, :] = x / x.sum(axis=1).reshape((-1, 1))
    return None

class _FixedPredictor:
    def __init__(self):
        pass

    def fit(self, X=None, y=None, sample_weight=None):
        pass

    def decision_function_w_sigmoid(self, X):
        return self.decision_function(X)

class _BetaPredictor(_FixedPredictor):
    def __init__(self, a, b, random_state):
        self.a = a
        self.b = b
        self.random_state = _check_random_state(random_state)

    def predict_proba(self, X):
        preds = self.random_state.beta(self.a, self.b, size = X.shape[0]).reshape((-1, 1))
        return np.c_[1.0 - preds, preds]

    def decision_function(self, X):
        return self.random_state.beta(self.a, self.b, size = X.shape[0])

    def predict(self, X):
        return (self.random_state.beta(self.a, self.b, size = X.shape[0])).astype('uint8')

    def exploit(self, X):
        return np.repeat(self.a / self.b, X.shape[0])

class _ZeroPredictor(_FixedPredictor):

    def predict_proba(self, X):
        return np.c_[np.ones((X.shape[0], 1)),  np.zeros((X.shape[0], 1))]

    def decision_function(self, X):
        return np.zeros(X.shape[0])

    def predict(self, X):
        return np.zeros(X.shape[0])

class _OnePredictor(_FixedPredictor):

    def predict_proba(self, X):
        return np.c_[np.zeros((X.shape[0], 1)),  np.ones((X.shape[0], 1))]

    def decision_function(self, X):
        return np.ones(X.shape[0])

    def predict(self, X):
        return np.ones(X.shape[0])

class _RandomPredictor(_FixedPredictor):
    def __init__(self, random_state):
        self.random_state = _check_random_state(random_state)

    def _gen_random(self, X):
        return self.random_state.random(size = X.shape[0])

    def predict(self, X):
        return (self._gen_random(X) >= .5).astype('uint8')

    def decision_function(self, X):
        return self._gen_random(X)

    def predict_proba(self, X):
        pred = self._gen_random(X)
        return np.c[pred, 1. - pred]

class _BootstrappedClassifierBase:
    def __init__(self, base, nsamples, percentile = 80, partialfit = False,
                 partial_method = "gamma", random_state = 1, njobs = 1):
        self.bs_algos = [deepcopy(base) for n in range(nsamples)]
        self.partialfit = partialfit
        self.partial_method = partial_method
        self.nsamples = nsamples
        self.percentile = percentile
        self.njobs = njobs
        self.random_state = _check_random_state(random_state)

    def fit(self, X, y):
        ix_take_all = self.random_state.integers(X.shape[0], size = (X.shape[0], self.nsamples))
        Parallel(n_jobs=self.njobs, verbose=0, require="sharedmem")\
                (delayed(self._fit_single)(sample, ix_take_all, X, y) \
                    for sample in range(self.nsamples))

    def _fit_single(self, sample, ix_take_all, X, y):
        ix_take = ix_take_all[:, sample]
        xsample = X[ix_take, :]
        ysample = y[ix_take]
        n_pos = (ysample > 0.).sum()
        if not self.partialfit:
            if n_pos == ysample.shape[0]:
                self.bs_algos[sample] = _OnePredictor()
                return None
            elif n_pos == 0:
                self.bs_algos[sample] = _ZeroPredictor()
                return None
            else:
                self.bs_algos[sample].fit(xsample, ysample)
        else:
            if (n_pos == ysample.shape[0]) or (n_pos == 0):
                self.bs_algos[sample].partial_fit(xsample, ysample, classes=[0,1])
            else:
                self.bs_algos[sample].fit(xsample, ysample)

    def partial_fit(self, X, y, classes=None):
        if self.partial_method == "gamma":
            w_all = self.random_state.standard_gamma(1, size = (X.shape[0], self.nsamples))
            appear_times = None
            rng = None
        elif self.partial_method == "poisson":
            w_all = None
            appear_times = self.random_state.poisson(1, size = (X.shape[0], self.nsamples))
            rng = np.arange(X.shape[0])
        else:
            raise ValueError(_unexpected_err_msg)
        Parallel(n_jobs=self.njobs, verbose=0, require="sharedmem")\
                (delayed(self._partial_fit_single)\
                    (sample, w_all, appear_times, rng, X, y) \
                        for sample in range(self.nsamples))

    def _partial_fit_single(self, sample, w_all, appear_times_all, rng, X, y):
        if w_all is not None:
            self.bs_algos[sample].partial_fit(X, y, classes=[0, 1], sample_weight=w_all[:, sample])
        elif appear_times_all is not None:
            appear_times = np.repeat(rng, appear_times_all[:, sample])
            xsample = X[appear_times]
            ysample = y[appear_times]
            self.bs_algos[sample].partial_fit(xsample, ysample, classes = [0, 1])
        else:
            raise ValueError(_unexpected_err_msg)

    def _score_max(self, X):
        pred = np.empty((X.shape[0], self.nsamples))
        Parallel(n_jobs=self.njobs, verbose=0, require="sharedmem")\
                (delayed(self._assign_score)(sample, pred, X) \
                    for sample in range(self.nsamples))
        return np.percentile(pred, self.percentile, axis=1)

    def _score_avg(self, X):
        ### Note: don't try to make it more memory efficient by summing to a single array,
        ### as otherwise it won't be multithreaded.
        pred = np.empty((X.shape[0], self.nsamples))
        Parallel(n_jobs=self.njobs, verbose=0, require="sharedmem")\
                (delayed(self._assign_score)(sample, pred, X) \
                    for sample in range(self.nsamples))
        return pred.mean(axis = 1)

    def _assign_score(self, sample, pred, X):
        pred[:, sample] = self._get_score(sample, X)

    def _score_rnd(self, X):
        chosen_sample = self.random_state.integers(self.nsamples)
        return self._get_score(chosen_sample, X)

    def exploit(self, X):
        return self._score_avg(X)

    def predict(self, X):
        ### Thompson sampling
        if self.percentile is None:
            pred = self._score_rnd(X)

        ### Upper confidence bound
        else:
            pred = self._score_max(X)

        return pred

class _BootstrappedClassifier_w_predict_proba(_BootstrappedClassifierBase):
    def _get_score(self, sample, X):
        return self.bs_algos[sample].predict_proba(X)[:, 1]

class _BootstrappedClassifier_w_decision_function(_BootstrappedClassifierBase):
    def _get_score(self, sample, X):
        pred = self.bs_algos[sample].decision_function(X).reshape(-1)
        _apply_sigmoid(pred)
        return pred

class _BootstrappedClassifier_w_predict(_BootstrappedClassifierBase):
    def _get_score(self, sample, X):
        return self.bs_algos[sample].predict(X).reshape(-1)

class _RefitBuffer:
    def __init__(self, n=50, deep_copy=False, random_state=1):
        self.n = n
        self.deep_copy = deep_copy
        self.curr = 0
        self.X_reserve = list()
        self.y_reserve = list()
        self.dim = 0
        self.random_state = _check_random_state(random_state)

    def add_obs(self, X, y):
        if X.shape[0] == 0:
            return None
        n_new = X.shape[0]
        if self.curr == 0:
            self.dim = X.shape[1]
        if X.shape[1] != self.dim:
            raise ValueError("Wrong number of columns for X.")

        if (self.curr == 0) and (self.deep_copy):
            self.X_reserve = np.empty((self.n, self.dim), dtype=X.dtype)
            self.y_reserve = np.empty(self.n, dtype=y.dtype)

        if (self.curr + n_new) <= (self.n):
            if isinstance(self.X_reserve, list):
                self.X_reserve.append(X)
                self.y_reserve.append(y)
                self.curr += n_new
                if self.curr == self.n:
                    self.X_reserve = np.concatenate(self.X_reserve, axis=0)
                    self.y_reserve = np.concatenate(self.y_reserve, axis=0)
            else:
                self.X_reserve[self.curr : self.curr + n_new] = X[:]
                self.y_reserve[self.curr : self.curr + n_new] = y[:]
                self.curr += n_new
        elif isinstance(self.X_reserve, list):
            self.X_reserve.append(X)
            self.y_reserve.append(y)
            self.X_reserve = np.concatenate(self.X_reserve, axis=0)
            self.y_reserve = np.concatenate(self.y_reserve, axis=0)
            keep = self.random_state.choice(self.X_reserve.shape[0], size=self.n, replace=False)
            self.X_reserve = self.X_reserve[keep]
            self.y_reserve = self.y_reserve[keep]
            self.curr = self.n
        elif self.curr < self.n:
            if n_new == self.n:
                self.X_reserve[:] = X[:]
                self.y_reserve[:] = y[:]
            else:
                diff = self.n - self.curr
                self.X_reserve[self.curr:] = X[:diff]
                self.y_reserve[self.curr:] = y[:diff]
                take_ix = self.random_state.choice(self.n+n_new-diff, size=self.n, replace=False)
                old_ix = take_ix[take_ix < self.n]
                new_ix = take_ix[take_ix >= self.n] - self.n + diff
                self.X_reserve = np.r_[self.X_reserve[old_ix], X[new_ix]]
                self.y_reserve = np.r_[self.y_reserve[old_ix], y[new_ix]]
            self.curr = self.n
        else: ### can only reach this point once reserve is full
            if n_new == self.n:
                self.X_reserve[:] = X[:]
                self.y_reserve[:] = y[:]
            elif n_new < self.n:
                replace_ix = self.random_state.choice(self.n, size=n_new, replace=False)
                self.X_reserve[replace_ix] = X[:]
                self.y_reserve[replace_ix] = y[:]
            else:
                take_ix = self.random_state.choice(self.n+n_new, size=self.n, replace=False)
                old_ix = take_ix[take_ix < self.n]
                new_ix = take_ix[take_ix >= self.n] - self.n
                self.X_reserve = np.r_[self.X_reserve[old_ix], X[new_ix]]
                self.y_reserve = np.r_[self.y_reserve[old_ix], y[new_ix]]

    def get_batch(self, X, y):
        if self.curr == 0:
            self.add_obs(X, y)
            return X, y

        if (self.curr < self.n) and (isinstance(self.X_reserve, list)):
            old_X = np.concatenate(self.X_reserve, axis=0)
            old_y = np.concatenate(self.y_reserve, axis=0)
        else:
            old_X = self.X_reserve[:self.curr].copy()
            old_y = self.y_reserve[:self.curr].copy()

        if X.shape[0] == 0:
            return old_X, old_y
        else:
            self.add_obs(X, y)

        return np.r_[old_X, X], np.r_[old_y, y]

    def do_full_refit(self):
        return self.curr < self.n

class _OneVsRest:
    def __init__(self, base,
                 X, a, r, n,
                 thr, alpha, beta,
                 random_state,
                 smooth=False, assume_un=False,
                 partialfit=False, refit_buffer=0, deep_copy=False,
                 force_fit=False, force_counters=False,
                 prev_ovr=None, warm=False,
                 njobs=1):
        self.n = n
        self.smooth = smooth
        self.assume_un = assume_un
        self.njobs = njobs
        self.force_fit = force_fit
        self.thr = thr
        self.random_state = random_state
        self.refit_buffer = refit_buffer
        self.deep_copy = deep_copy
        self.partialfit = bool(partialfit)
        self.force_counters = bool(force_counters)
        if self.force_counters or (self.thr > 0 and not self.force_fit):
            ## in case it has beta prior, keeps track of the counters until no longer needed
            self.alpha = alpha
            self.beta = beta

            ## beta counters are represented as follows:
            # * first row: whether it shall use the prior
            # * second row: number of positives
            # * third row: number of negatives
            self.beta_counters = np.zeros((3, n))

        if self.smooth is not None:
            self.counters = np.zeros((1, n)) ##counters are row vectors to multiply them later with pred matrix
        else:
            self.counters = None

        if self.random_state == np.random:
            self.rng_arm = [self.random_state] * self.n
        elif prev_ovr is None:
            self.rng_arm = \
                [_check_random_state(
                        self.random_state.integers(np.iinfo(np.int32).max) + 1) \
                    for choice in range(self.n)]
        else:
            self.rng_arm = prev_ovr.rng_arm

        if (refit_buffer is not None) and (refit_buffer > 0):
            self.buffer = [_RefitBuffer(refit_buffer, deep_copy, self.rng_arm[choice]) \
                            for choice in range(n)]
        else:
            self.buffer = None

        if 'predict_proba' not in dir(base):
            base = _convert_decision_function_w_sigmoid(base)
        if partialfit:
            base = _add_method_predict_robust(base)
        if isinstance(base, list):
            self.base = None
            self.algos = base
        else:
            self.base = base
            if prev_ovr is not None:
                self.algos = prev_ovr.algos
                for choice in range(self.n):
                    if isinstance(self.algos[choice], _FixedPredictor):
                        self.algos[choice] = deepcopy(base)
            else: 
                self.algos = [deepcopy(base) for choice in range(self.n)]
                if isinstance(base, _BootstrappedClassifierBase) \
                   or isinstance(base, _LinUCB_n_TS_single) \
                   or isinstance(base, _LogisticUCB_n_TS_single) \
                   or isinstance(base, _BayesianLogisticRegression):
                    for choice in range(self.n):
                        self.algos[choice].random_state = self.rng_arm[choice]

        if self.partialfit:
            self.partial_fit(X, a, r)
        else:
            Parallel(n_jobs=self.njobs, verbose=0, require="sharedmem")\
                    (delayed(self._full_fit_single)\
                            (choice, X, a, r) for choice in range(self.n))

    def _drop_arm(self, drop_ix):
        del self.algos[drop_ix]
        del self.rng_arm[drop_ix]
        if self.buffer is not None:
            del sef.buffer[drop_ix]
        self.n -= 1
        if self.smooth is not None:
            self.counters = self.counters[:, np.arange(self.counters.shape[1]) != drop_ix]
        if self.force_counters or (self.thr > 0 and not self.force_fit):
            self.beta_counters = self.beta_counters[:, np.arange(self.beta_counters.shape[1]) != drop_ix]

    def _spawn_arm(self, fitted_classifier = None, n_w_rew = 0, n_wo_rew = 0,
                   buffer_X = None, buffer_y = None):
        self.n += 1
        self.rng_arm.append(self.random_state if (self.random_state == np.random) else \
                            _check_random_state(
                                self.random_state.integers(np.iinfo(np.int32).max) + 1))
        if self.smooth is not None:
            self.counters = np.c_[self.counters, np.array([n_w_rew + n_wo_rew]).reshape((1, 1)).astype(self.counters.dtype)]
        if self.force_counters or (self.thr > 0 and not self.force_fit):
            new_beta_col = np.array([0 if (n_w_rew + n_wo_rew) < self.thr else 1, self.alpha + n_w_rew, self.beta + n_wo_rew]).reshape((3, 1)).astype(self.beta_counters.dtype)
            self.beta_counters = np.c_[self.beta_counters, new_beta_col]
        if fitted_classifier is not None:
            if 'predict_proba' not in dir(fitted_classifier):
                fitted_classifier = _convert_decision_function_w_sigmoid(fitted_classifier)
            if partialfit:
                fitted_classifier = _add_method_predict_robust(fitted_classifier)
            self.algos.append(fitted_classifier)
        else:
            if self.force_fit or self.partialfit:
                if self.base is None:
                    raise ValueError("Must provide a classifier when initializing with different classifiers per arm.")
                self.algos.append( deepcopy(self.base) )
            else:
                if self.force_counters or (self.thr > 0 and not self.force_fit):
                    self.algos.append(_BetaPredictor(self.beta_counters[:, -1][1],
                                                     self.beta_counters[:, -1][2],
                                                     self.rng_arm[-1]))
                else:
                    self.algos.append(_ZeroPredictor())
        if (self.buffer is not None):
            self.buffer.append(_RefitBuffer(self.refit_buffer, self.deep_copy,
                                            self.rng_arm[-1]))
            if (buffer_X is not None):
                self.buffer[-1].add_obs(bufferX, buffer_y)

    def _update_beta_counters(self, yclass, choice):
        if (self.beta_counters[0, choice] == 0) or self.force_counters:
            n_pos = (yclass > 0.).sum()
            self.beta_counters[1, choice] += n_pos
            self.beta_counters[2, choice] += yclass.shape[0] - n_pos
            if (self.beta_counters[1, choice] > self.thr) and (self.beta_counters[2, choice] > self.thr):
                self.beta_counters[0, choice] = 1

    def _full_fit_single(self, choice, X, a, r):
        yclass, this_choice = self._filter_arm_data(X, a, r, choice)
        n_pos = (yclass > 0.).sum()
        if self.smooth is not None:
            self.counters[0, choice] += yclass.shape[0]
        if (n_pos < self.thr) or ((yclass.shape[0] - n_pos) < self.thr):
            if not self.force_fit:
                self.algos[choice] = _BetaPredictor(self.alpha + n_pos,
                                                    self.beta + yclass.shape[0] - n_pos,
                                                    self.rng_arm[choice])
                return None
        if n_pos == 0:
            if not self.force_fit:
                self.algos[choice] = _ZeroPredictor()
                return None
        if n_pos == yclass.shape[0]:
            if not self.force_fit:
                self.algos[choice] = _OnePredictor()
                return None
        xclass = X[this_choice, :]
        self.algos[choice].fit(xclass, yclass)

        if self.force_counters or (self.thr > 0 and not self.force_fit):
            self._update_beta_counters(yclass, choice)


    def partial_fit(self, X, a, r):
        Parallel(n_jobs=self.njobs, verbose=0, require="sharedmem")\
                (delayed(self._partial_fit_single)(choice, X, a, r) \
                    for choice in range(self.n))

    def _partial_fit_single(self, choice, X, a, r):
        yclass, this_choice = self._filter_arm_data(X, a, r, choice)
        if self.smooth is not None:
            self.counters[0, choice] += yclass.shape[0]

        xclass = X[this_choice, :]
        do_full_refit = False
        if self.buffer is not None:
            do_full_refit = self.buffer[choice].do_full_refit()
            xclass, yclass = self.buffer[choice].get_batch(xclass, yclass)

        if (xclass.shape[0] > 0) or self.force_fit:
            if (do_full_refit) and (np.unique(yclass).shape[0] >= 2):
                self.algos[choice].fit(xclass, yclass)
            else:
                self.algos[choice].partial_fit(xclass, yclass, classes = [0, 1])

        ## update the beta counters if needed
        if self.force_counters:
            self._update_beta_counters(yclass, choice)

    def _filter_arm_data(self, X, a, r, choice):
        if self.assume_un:
            this_choice = (a == choice)
            arms_w_rew = (r > 0.)
            yclass = r[this_choice | arms_w_rew]
            yclass[arms_w_rew & (~this_choice) ] = 0
            this_choice = this_choice | arms_w_rew
        else:
            this_choice = (a == choice)
            yclass = r[this_choice]

        ## Note: don't filter X here as in many cases it won't end up used
        return yclass, this_choice

    ### TODO: these parallelizations probably shouldn't use sharedmem,
    ### but they still need to somehow modify the random states
    def decision_function(self, X):
        preds = np.zeros((X.shape[0], self.n))
        Parallel(n_jobs=self.njobs, verbose=0, require="sharedmem")\
                (delayed(self._decision_function_single)(choice, X, preds, 1) \
                    for choice in range(self.n))
        _apply_smoothing(preds, self.smooth, self.counters)
        return preds

    def _decision_function_single(self, choice, X, preds, depth=2):
        ## case when using partial_fit and need beta predictions
        if (self.partialfit or self.force_fit) and (self.thr > 0):
            if self.beta_counters[0, choice] == 0:
                preds[:, choice] = \
                    self.rng_arm[choice].beta(self.alpha + self.beta_counters[1, choice],
                                              self.beta  + self.beta_counters[2, choice],
                                              size=preds.shape[0])
                return None

        if 'predict_proba_robust' in dir(self.algos[choice]):
            preds[:, choice] = self.algos[choice].predict_proba_robust(X)[:, 1]
        elif 'predict_proba' in dir(self.base):
            preds[:, choice] = self.algos[choice].predict_proba(X)[:, 1]
        else:
            if depth == 0:
                raise ValueError("This requires a classifier with method 'predict_proba'.")
            if 'decision_function_robust' in dir(self.algos[choice]):
                preds[:, choice] = self.algos[choice].decision_function_robust(X)
            elif 'decision_function_w_sigmoid' in dir(self.algos[choice]):
                preds[:, choice] = self.algos[choice].decision_function_w_sigmoid(X)
            else:
                preds[:, choice] = self.algos[choice].predict(X)

        ### Note to self: it's not a problem to mix different methods from the
        ### base class and from the fixed predictors class (e.g.
        ### 'decision_function' from base vs. 'predict_proba' from fixed predictor),
        ### because the base's method get standardized beforehand through
        ### '_convert_decision_function_w_sigmoid'.

    def predict_proba(self, X):
        ### this is only used for softmax explorer
        preds = np.zeros((X.shape[0], self.n))
        Parallel(n_jobs=self.njobs, verbose=0, require="sharedmem")\
                (delayed(self._decision_function_single)(choice, X, preds, 1) \
                    for choice in range(self.n))
        _apply_smoothing(preds, self.smooth, self.counters)
        _apply_inverse_sigmoid(preds)
        _apply_softmax(preds)
        return preds

    def predict_proba_raw(self,X):
        preds = np.zeros((X.shape[0], self.n))
        Parallel(n_jobs=self.njobs, verbose=0, require="sharedmem")\
                (delayed(self._decision_function_single)(choice, X, preds, 0) \
                    for choice in range(self.n))
        _apply_smoothing(preds, self.smooth, self.counters)
        return preds

    def predict(self, X):
        return np.argmax(self.decision_function(X), axis=1)

    def should_calculate_grad(self, choice):
        if self.force_fit:
            return True
        if isinstance(self.algos[choice], _FixedPredictor):
            return False
        if not bool(self.thr):
            return True
        try:
            return bool(self.beta_counters[0, choice])
        except:
            return True

    def get_n_pos(self, choice):
        return self.beta_counters[1, choice]

    def get_n_neg(self, choice):
        return self.beta_counters[2, choice]

    def exploit(self, X):
        ### only usable within some policies
        pred = np.empty((X.shape[0], self.n))
        Parallel(n_jobs=self.njobs, verbose=0, require="sharedmem")\
                (delayed(self._exploit_single)(choice, pred, X) \
                    for choice in range(self.n))
        return pred

    def _exploit_single(self, choice, pred, X):
        pred[:, choice] = self.algos[choice].exploit(X)


class _BayesianLogisticRegression:
    def __init__(self, method='advi', niter=2000, nsamples=50,
                 mode='ucb', perc=None, random_state=1):
        #TODO: reimplement with something faster than using PyMC3's black-box methods
        self.nsamples = nsamples
        self.niter = niter
        self.mode = mode
        self.perc = perc
        self.method = method
        self.random_state = _check_random_state(random_state)

    def fit(self, X, y):
        try:
            import pymc3 as pm
        except:
            msg  = "This method requires PyCM3. "
            msg += "Can be installed with e.g. 'pip install pymc3'."
            raise ValueError(msg)
        import pandas as pd
        import logging
        logger = logging.getLogger("pymc3")
        logger.propagate = False
        with pm.Model():
            pm.glm.linear.GLM(X, y, family = 'binomial')
            if self.method == 'advi':
                trace = pm.fit(progressbar = False, n = self.niter,
                               random_seed = self.random_state.integers(
                                                np.iinfo(np.int32).max + 1
                                            ))
            else:
                trace = pm.sample(progressbar = False, draws = self.niter, tune = 500,
                                  step = None if self.method == "nuts" else pm.Metropolis(),
                                  random_seed = self.random_state.integers(
                                                np.iinfo(np.int32).max + 1
                                            ))
        if self.method == 'advi':
            self.coefs = [i for i in trace.sample(self.nsamples)]
            self.coefs = pd.DataFrame.from_dict(self.coefs)
        else:
            samples_chosen = self.random_state.choice(len(trace),
                                                      size = self.nsamples,
                                                      replace = False)
            self.coefs = [i for i in trace]
            self.coefs = pd.DataFrame.from_dict(self.coefs)
            self.coefs = self.coefs.iloc[samples_chosen]
        self.coefs = self.coefs[ ['Intercept'] + ['x' + str(i) for i in range(X.shape[1])] ]
        self.intercept = self.coefs['Intercept'].values.reshape((1, -1)).copy()
        del self.coefs['Intercept']
        self.coefs = self.coefs.to_numpy().T

    ### TODO: implement 'partial_fit' with stochastic variational inference

    def _predict_all(self, X):
        pred_all = X.dot(self.coefs) + self.intercept
        _apply_sigmoid(pred_all)
        return pred_all

    def predict(self, X, exploit = False):
        pred = self._predict_all(X)
        if exploit:
            return pred.mean(axis = 1)
        elif self.mode == 'ucb':
            pred = np.percentile(pred, self.perc, axis=1)
        elif self.mode == 'ts':
            pred = pred[:, self.random_state.integers(pred.shape[1])]
        else:
            raise ValueError(_unexpected_err_msg)
        return pred

    def exploit(self, X):
        return self.predict(X, exploit = True)

class _LinUCB_n_TS_single:
    def __init__(self, alpha=1.0, lambda_=1.0, fit_intercept=True,
                 use_float=True, method="sm", ts=False,
                 sample_unique=False, random_state=1):
        self.alpha = alpha
        self.lambda_ = lambda_
        self.fit_intercept = fit_intercept
        self.use_float = use_float
        self.method = method
        self.ts = ts
        self.sample_unique = bool(sample_unique)
        self.random_state = _check_random_state(random_state)
        self.model = LinearRegression(lambda_=self.lambda_,
                                      fit_intercept=self.fit_intercept,
                                      method=self.method,
                                      use_float=self.use_float)

    def fit(self, X, y):
        if X.shape[0]:
            self.model.fit(X, y)
        return self

    def partial_fit(self, X, y, *args, **kwargs):
        if X.shape[0]:
            self.model.partial_fit(X, y)
        return self

    def predict(self, X, exploit=False):
        if exploit:
            return self.model.predict(X)
        elif not self.ts:
            return self.model.predict_ucb(X, self.alpha)
        else:
            return self.model.predict_thompson(X, self.alpha, self.sample_unique,
                                               self.random_state)

    def exploit(self, X):
        return self.predict(X, exploit = True)

class _LogisticUCB_n_TS_single:
    def __init__(self, lambda_=1., fit_intercept=True, alpha=0.95,
                 m=1.0, ts=False, sample_unique=False, random_state=1):
        self.conf_coef = norm_dist.ppf(alpha)
        self.m = m
        self.fit_intercept = fit_intercept
        self.lambda_ = lambda_
        self.ts = ts
        self.warm_start = True
        self.sample_unique = bool(sample_unique)
        self.random_state = _check_random_state(random_state)
        self.model = LogisticRegression(C=1./lambda_, penalty="l2",
                                        fit_intercept=fit_intercept,
                                        solver='lbfgs', max_iter=15000,
                                        warm_start=True)
        self.Sigma = np.empty((0,0), dtype=np.float64)

    def fit(self, X, y, *args, **kwargs):
        self.model.fit(X, y)
        var = self.model.predict_proba(X)[:,1]
        var = var * (1 - var)   
        n = X.shape[1]
        self.Sigma = np.zeros((n+self.fit_intercept, n+self.fit_intercept), dtype=ctypes.c_double)
        X, Xcsr = self._process_X(X)
        _wrapper_double.update_matrices_noinv(
            X,
            np.empty(0, dtype=ctypes.c_double),
            var,
            self.Sigma,
            np.empty(0, dtype=ctypes.c_double),
            Xcsr = Xcsr,
            add_bias=self.fit_intercept,
            overwrite=1
        )
        _matrix_inv_symm(self.Sigma, self.lambda_)

    def _process_X(self, X):
        if X.dtype != ctypes.c_double:
            X = X.astype(ctypes.c_double)
        if issparse(X):
            Xcsr = X
            X = np.empty((0,0), dtype=ctypes.c_double)
        else:
            Xcsr = None
        return X, Xcsr

    def decision_function(self, X, exploit=False):
        ### Thompson sampling
        if (self.ts) and (not exploit):
            if self.fit_intercept:
                coef = np.r_[self.model.coef_.reshape(-1), self.model.intercept_]
            else:
                coef = self.model.coef_.reshape(-1)

            tol = 1e-20
            if np.linalg.det(self.Sigma) >= tol:
                cov = self.Sigma
            else:
                cov = self.Sigma.copy()
                n = cov.shape[1]
                for i in range(10):
                    cov[np.arange(n), np.arange(n)] += 1e-1
                    if np.linalg.det(cov) >= tol:
                        break

            if self.sample_unique:
                coef = self.random_state.multivariate_normal(mean=coef,
                                                             cov=self.m * cov,
                                                             size=X.shape[0])
                if not issparse(X):
                    pred = np.einsum("ij,ij->i", X, coef[:,:X.shape[1]])
                else:
                    pred = X.multiply(coef[:,:X.shape[1]]).sum(axis=1)
                if self.fit_intercept:
                    pred[:] += coef[:,-1]
            else:
                coef = self.random_state.multivariate_normal(mean=coef,
                                                             cov=self.m * cov)
                pred = X.dot(coef[:X.shape[1]])
                if self.fit_intercept:
                    pred[:] += coef[-1]
            return pred

        ### UCB
        pred = self.model.decision_function(X)
        if not exploit:
            X, Xcsr = self._process_X(X)
            se_sq = _wrapper_double.x_A_x_batch(X, self.Sigma, Xcsr, self.fit_intercept, 1)
            pred[:] += self.conf_coef * np.sqrt(se_sq.reshape(-1))
        return pred

    def exploit(self, X):
        return 1. / (1. + np.exp(-self.model.decision_function(X)))
