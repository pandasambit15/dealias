"""
Module containing all the functions used for dealiasing. These functions use
radial-to-radial continuity, gate-to-gate continuity, box check, least square
continuity, ...

JIT-friendly is my excuse for a lot of function containing loops or
structure controls to make the function compatible with the Just-In-Time (JIT)
compiler of numba while they are sometimes shorter pythonic ways to do things.

@title: continuity
@author: Valentin Louf <valentin.louf@monash.edu>
@institutions: Monash University and the Australian Bureau of Meteorology
@date: 23/09/2019
"""
import numpy as np
from numba import jit, int64, float64


@jit(nopython=True)
def linregress(x, y):
    """
    Linear regression is an approach for predicting a response using a single
    feature. It is assumed that the two variables are linearly related. Hence,
    we try to find a linear function that predicts the response value(y) as
    accurately as possible as a function of the feature or independent
    variable(x).

    Parameters:
    ===========
        x: ndarray <vector>
        y: ndarray <vector>

    Returns:
    ========
        slope
        intecept
    """
    # number of observations/points
    n = len(x)

    # mean of x and y vector
    m_x, m_y = np.mean(x), np.mean(y)

    # calculating cross-deviation and deviation about x
    SS_xy = np.sum(y * x) - n * m_y * m_x
    SS_xx = np.sum(x * x) - n * m_x * m_x

    # calculating regression coefficients
    slope = SS_xy / SS_xx
    intercept = m_y - slope * m_x

    return slope, intercept


@jit(nopython=True)
def unfold(v1, v2, vnyq):
    """
    Compare two velocities, look at all possible unfolding value (up to a period
    of 7 times the nyquist) and find the unfolded velocity that is the closest
    the to reference.

    Parameters:
    ===========
    v1: float
        Reference velocity
    v2: float
        Velocity to unfold
    vnyq: float
        Nyquist velocity

    Returns:
    ========
    return voff[pos]
        vtrue: float
            Dealiased velocity.
    """
    n = np.arange(0, 7, 2)
    if v1 > 0:
        voff = v1 + (n * vnyq - np.abs(v1 - v2))
    else:
        voff = v1 - (n * vnyq - np.abs(v1 - v2))

    pos = np.argmin(np.abs(voff - v1))
    return voff[pos]


@jit(nopython=True)
def is_good_velocity(vel1, vel2, vnyq, alpha=0.8):
    """
    Compare two velocities, and check if they are comparable to each other.

    Parameters:
    ===========
    vel1: float
        Reference velocity
    vel2: float
        Velocity to unfold
    vnyq: float
        Nyquist velocity
    alpha: float
        Coefficient for which the nyquist velocity periodicity range is
        considered valid.

    Returns:
    ========
    True/False
    """
    return np.abs(vel2 - vel1) < alpha * vnyq


@jit(nopython=True)
def get_iter_pos(azi, st, nb=180):
    """
    Return a sequence of integers from start (inclusive) to stop (start + nb)
    by step of 1 for iterating over the azimuth (handle the case that azimuth
    360 is in fact 0, i.e. a cycle).
    JIT-friendly function (this is why the function looks much longer than the
    pythonic way of doing this).

    Parameters:
    ===========
    azi: ndarray<float>
        Azimuth.
    st: int
        Starting point.
    nb: int
        Number of unitary steps.

    Returns:
    ========
    out: ndarray<int>
        Array containing the position from start to start + nb, i.e.
        azi[out[0]] <=> st
    """
    if st < 0:
        st += len(azi)
    if st >= len(azi):
        st -= len(azi)

    ed = st + nb
    if ed >= len(azi):
        ed -= len(azi)
    if ed < 0:
        ed += len(azi)

    posazi = np.arange(0, len(azi))
    mypos = np.empty_like(posazi)

    if nb > 0:
        if st < ed:
            end = ed - st
            mypos[:end] = posazi[st:ed]
        else:
            mid = (len(azi) - st)
            end = (len(azi) - st + ed)
            mypos[:mid] = posazi[st:]
            mypos[mid:end] = posazi[:ed]
    else:  # Goin backward.
        if st < ed:
            mid = st + 1
            end = st + len(azi) - ed
            mypos[:mid] = posazi[st::-1]
            mypos[mid:end] = posazi[-1:ed:-1]
        else:
            end = np.abs(st - ed)
            mypos[:end] = posazi[st:ed:-1]

    out = np.zeros((end, ), dtype=mypos.dtype)
    for n in range(end):
        out[n] = mypos[n]

    return out


@jit(nopython=True)
def get_iter_range(pos_center, nb_gate, maxrange):
    """
    Similar as get_iter_pos, but this time for creating an array of iterative
    indices over the radar range. JIT-friendly function.

    Parameters:
    ===========
    pos_center: int
        Starting point
    nb_gate: int
        Number of gates to iter to.
    maxrange: int
        Length of the radar range, i.e. maxrange = len(r)

    Returns:
    ========
    Array of iteration indices.
    """
    half_range = nb_gate // 2
    if pos_center < half_range:
        st_pos = 0
    else:
        st_pos = pos_center - half_range

    if pos_center + half_range >= maxrange:
        end_pos = maxrange
    else:
        end_pos = pos_center + half_range

    return np.arange(st_pos, end_pos)


@jit(int64(float64, float64, float64, float64), nopython=True)
def take_decision(velocity_reference, velocity_to_check, vnyq, alpha):
    """
    Make a decision after comparing two velocities.

    Parameters:
    ===========
    velocity_to_check: float
        what we want to check
    velocity_reference: float
        reference

    Returns:
    ========
    -3: missing data (velocity we want to check does not exist)
    0: missing data (velocity used as reference does not exist)
    1: velocity is perfectly fine.
    2: velocity is folded.
    """
    if np.isnan(velocity_to_check):
        return -3
    elif np.isnan(velocity_reference):
        return 0
    elif (is_good_velocity(velocity_reference, velocity_to_check, vnyq, alpha=alpha) or
          (np.sign(velocity_reference) == np.sign(velocity_to_check))):
        return 1
    else:
        return 2


@jit(nopython=True)
def correct_clockwise(r, azi, vel, final_vel, flag_vel, myquadrant, vnyq, window_len=3, alpha=0.8):
    """
    Dealias using strict radial-to-radial continuity. The previous 3 radials are
    used as reference. Clockwise means that we loop over increasing azimuth
    (which is in fact counterclockwise, but let's try not to be confusing).
    This function will look at unprocessed velocity only.
    In this version of the code, if no radials are found in continuity, then we
    we use the gate to gate continuity.

    Parameters:
    ===========
    r: ndarray
        Radar scan range.
    azi: ndarray
        Radar scan azimuth.
    vel: ndarray <azimuth, r>
        Aliased Doppler velocity field.
    final_vel: ndarray <azimuth, r>
        Dealiased Doppler velocity field.
    flag_vel: ndarray int <azimuth, range>
        Flag array -3: No data, 0: Unprocessed, 1: good as is, 2: dealiased.
    myquadrant: ndarray <int>
        Position of azimuth to iter upon.
    nyquist_velocity: float
        Nyquist velocity.

    Returns:
    ========
    dealias_vel: ndarray <azimuth, range>
        Dealiased velocity slice.
    flag_vel: ndarray int <azimuth, range>
        Flag array -3: No data, 0: Unprocessed, 1: good as is, 2: dealiased.
    """
    maxgate = len(r)
    flag_threshold = window_len // 3
    if flag_threshold == 0:
        flag_threshold = 1
    # the number 3 is because we use the previous 3 radials as reference.
    for nbeam in myquadrant[window_len:]:
        for ngate in range(0, maxgate):
            # Check if already unfolded
            if flag_vel[nbeam, ngate] != 0:
                continue

            # We want the previous 3 radials.
            npos = nbeam - window_len
            # Unfolded velocity
            velref = final_vel[get_iter_pos(azi, npos, window_len), ngate]
            flagvelref = flag_vel[get_iter_pos(azi, npos, window_len), ngate]

            # Folded velocity
            vel1 = vel[nbeam, ngate]

            if np.sum(flagvelref > 0) < flag_threshold:
                continue

            mean_vel_ref = np.mean(velref[(flagvelref == 1) | (flagvelref == 2)])

            decision = take_decision(mean_vel_ref, vel1, vnyq, alpha=alpha)

            # If loose, skip this test.
            if ngate != 0 and window_len <= 3:
                npos = ngate - 1
                mean_vel_ref2 = final_vel[nbeam, npos]

                decision2 = take_decision(mean_vel_ref2, vel1, vnyq, alpha=alpha)
                if decision != decision2:
                    continue

            if decision == -3:
                flag_vel[nbeam, ngate] = -3
                continue
            elif decision == 1:
                final_vel[nbeam, ngate] = vel1
                flag_vel[nbeam, ngate] = 1
                continue
            elif decision == 2:
                vtrue = unfold(mean_vel_ref, vel1, vnyq)
                if is_good_velocity(mean_vel_ref, vtrue, vnyq, alpha=alpha):
                    final_vel[nbeam, ngate] = vtrue
                    flag_vel[nbeam, ngate] = 2

    return final_vel, flag_vel


@jit(nopython=True)
def correct_counterclockwise(r, azi, vel, final_vel, flag_vel, myquadrant, vnyq,
                             window_len=3, alpha=0.8):
    """
    Dealias using strict radial-to-radial continuity. The next 3 radials are
    used as reference. Counterclockwise means that we loop over decreasing
    azimuths (which is in fact clockwise... I know, it's confusing).
    This function will look at unprocessed velocity only.
    In this version of the code, if no radials are found in continuity, then we
    we use the gate to gate continuity.

    Parameters:
    ===========
    r: ndarray
        Radar scan range.
    azi: ndarray
        Radar scan azimuth.
    vel: ndarray <azimuth, r>
        Aliased Doppler velocity field.
    final_vel: ndarray <azimuth, r>
        Dealiased Doppler velocity field.
    flag_vel: ndarray int <azimuth, range>
        Flag array -3: No data, 0: Unprocessed, 1: good as is, 2: dealiased.
    myquadrant: ndarray <int>
        Position of azimuth to iter upon.
    vnyq: float
        Nyquist velocity.

    Returns:
    ========
    dealias_vel: ndarray <azimuth, range>
        Dealiased velocity slice.
    flag_vel: ndarray int <azimuth, range>
        Flag array -3: No data, 0: Unprocessed, 1: good as is, 2: dealiased.
    """
    maxgate = len(r)
    flag_threshold = window_len // 3
    if flag_threshold == 0:
        flag_threshold = 1

    for nbeam in myquadrant:
        for ngate in range(0, maxgate):
            # Check if already unfolded
            if flag_vel[nbeam, ngate] != 0:
                continue

            # We want the next 3 radials.
            npos = nbeam + 1
            # Unfolded velocity.
            velref = final_vel[get_iter_pos(azi, npos, window_len), ngate]
            flagvelref = flag_vel[get_iter_pos(azi, npos, window_len), ngate]

            # Folded velocity
            vel1 = vel[nbeam, ngate]

            if np.sum(flagvelref > 0) < flag_threshold:
                continue

            mean_vel_ref = np.mean(velref[(flagvelref == 1) | (flagvelref == 2)])

            decision = take_decision(mean_vel_ref, vel1, vnyq, alpha=alpha)

            # If loose, skip this test.
            if ngate != 0 and window_len <= 3:
                npos = ngate - 1
                mean_vel_ref2 = final_vel[nbeam, npos]

                decision2 = take_decision(mean_vel_ref2, vel1, vnyq, alpha=alpha)
                if decision != decision2:
                    continue

            if decision == -3:
                flag_vel[nbeam, ngate] = -3
                continue
            elif decision == 1:
                final_vel[nbeam, ngate] = vel1
                flag_vel[nbeam, ngate] = 1
                continue
            elif decision == 2:
                vtrue = unfold(mean_vel_ref, vel1, vnyq)
                if is_good_velocity(mean_vel_ref, vtrue, vnyq, alpha=alpha):
                    final_vel[nbeam, ngate] = vtrue
                    flag_vel[nbeam, ngate] = 2

    return final_vel, flag_vel


@jit(nopython=True)
def correct_range_onward(vel, final_vel, flag_vel, vnyq, window_len=6, alpha=0.8):
    """
    Dealias using strict gate-to-gate continuity. The directly previous gate
    is used as reference. This function will look at unprocessed velocity only.

    Parameters:
    ===========
    vel: ndarray <azimuth, r>
        Aliased Doppler velocity field.
    final_vel: ndarray <azimuth, r>
        Dealiased Doppler velocity field.
    flag_vel: ndarray int <azimuth, range>
        Flag array -3: No data, 0: Unprocessed, 1: good as is, 2: dealiased.
    vnyq: float
        Nyquist velocity.

    Returns:
    ========
    dealias_vel: ndarray <azimuth, range>
        Dealiased velocity slice.
    flag_vel: ndarray int <azimuth, range>
        Flag array -3: No data, 0: Unprocessed, 1: good as is, 2: dealiased.
    """
    flag_threshold = window_len // 3
    if flag_threshold == 0:
        flag_threshold = 1

    maxazi, maxrange = final_vel.shape
    for nbeam in range(maxazi):
        for ngate in range(1, maxrange):
            if flag_vel[nbeam, ngate] != 0:
                continue

            vel1 = vel[nbeam, ngate]
            npos = ngate - 1
            velref = final_vel[nbeam, npos]
            flagvelref = flag_vel[nbeam, npos]

            if flagvelref <= 0:
                if ngate < window_len:
                    continue

                velref_vec = final_vel[nbeam, (ngate - window_len):ngate]
                flagvelref_vec = flag_vel[nbeam, (ngate - window_len):ngate]
                if np.sum(flagvelref_vec > 0) < flag_threshold:
                    continue

                velref = np.nanmean(velref_vec[flagvelref_vec > 0])

            decision = take_decision(velref, vel1, vnyq, alpha=alpha)

            if decision == 1:
                final_vel[nbeam, ngate] = vel1
                flag_vel[nbeam, ngate] = 1
                continue
            elif decision == 2:
                vtrue = unfold(velref, vel1, vnyq)
                if is_good_velocity(velref, vtrue, vnyq, alpha=alpha):
                    final_vel[nbeam, ngate] = vtrue
                    flag_vel[nbeam, ngate] = 2

    return final_vel, flag_vel


@jit(nopython=True)
def correct_range_backward(vel, final_vel, flag_vel, vnyq, window_len=6, alpha=0.8):
    """
    Dealias using strict gate-to-gate continuity. The directly next gate (going
    backward, i.e. from the outside to the center) is used as reference.
    This function will look at unprocessed velocity only.

    Parameters:
    ===========
    vel: ndarray <azimuth, r>
        Aliased Doppler velocity field.
    final_vel: ndarray <azimuth, r>
        Dealiased Doppler velocity field.
    flag_vel: ndarray int <azimuth, range>
        Flag array -3: No data, 0: Unprocessed, 1: good as is, 2: dealiased.
    vnyq: float
        Nyquist velocity.

    Returns:
    ========
    dealias_vel: ndarray <azimuth, range>
        Dealiased velocity slice.
    flag_vel: ndarray int <azimuth, range>
        Flag array -3: No data, 0: Unprocessed, 1: good as is, 2: dealiased.
    """
    flag_threshold = window_len // 3
    if flag_threshold == 0:
        flag_threshold = 1

    for nbeam in range(vel.shape[0]):
        start_vec = np.where(flag_vel[nbeam, :] == 1)[0]
        if len(start_vec) == 0:
            continue

        start_gate = start_vec[-1]
        for ngate in np.arange(start_gate - (window_len + 1), window_len, -1):
            if flag_vel[nbeam, ngate] != 0:
                continue

            vel1 = vel[nbeam, ngate]
            npos = ngate + 1
            velref = final_vel[nbeam, npos]
            flagvelref = flag_vel[nbeam, npos]

            if flagvelref <= 0:
                if ngate + window_len >= vel.shape[1]:
                    # Out of range.
                    continue

                velref_vec = final_vel[nbeam, ngate:(ngate + window_len)]
                flagvelref_vec = flag_vel[nbeam, ngate:(ngate + window_len)]
                if np.sum(flagvelref_vec > 0) < flag_threshold:
                    continue

                velref = np.nanmean(velref_vec[flagvelref_vec > 0])

            decision = take_decision(velref, vel1, vnyq, alpha=alpha)

            if decision == 1:
                final_vel[nbeam, ngate] = vel1
                flag_vel[nbeam, ngate] = 1
                continue
            elif decision == 2:
                vtrue = unfold(velref, vel1, vnyq)
                if is_good_velocity(velref, vtrue, vnyq, alpha=alpha):
                    final_vel[nbeam, ngate] = vtrue
                    flag_vel[nbeam, ngate] = 2

    return final_vel, flag_vel


@jit(nopython=True)
def correct_linear_interp(velocity, final_vel, flag_vel, vnyq, r_step=200, alpha=0.8):
    """
    Dealias using data close to the radar as reference for the most distant
    points left to dealiase.

    Parameters:
    ===========
    velocity: ndarray <azimuth, r>
        Aliased Doppler velocity field.
    final_vel: ndarray <azimuth, r>
        Dealiased Doppler velocity field.
    flag_vel: ndarray int <azimuth, range>
        Flag array -3: No data, 0: Unprocessed, 1: good as is, 2: dealiased.
    vnyq: float
        Nyquist velocity.
    r_step: int
        Number of gates used to compute reference.

    Returns:
    ========
    dealias_vel: ndarray <azimuth, range>
        Dealiased velocity slice.
    flag_vel: ndarray int <azimuth, range>
        Flag array -3: No data, 0: Unprocessed, 1: good as is, 2: dealiased.
    """
    maxazi, maxrange = final_vel.shape
    for nbeam in range(maxazi):
        if not np.any((flag_vel[nbeam, r_step:] == 0)):
            # There is nothing left to process for this azimuth.
            continue

        pos = flag_vel[nbeam, :r_step] > 0
        if np.sum(pos) == 0:
            # There's nothing that can be used as reference.
            continue

        v_selected = final_vel[nbeam, :r_step][pos]
        vmoy = np.mean(v_selected)

        if np.any((v_selected > 0)):
            vmoy_plus = np.nanmean(v_selected[v_selected > 0])
        else:
            vmoy_plus = np.NaN
        if np.any((v_selected < 0)):
            vmoy_minus = np.nanmean(v_selected[v_selected < 0])
        else:
            vmoy_minus = np.NaN

        if np.isnan(vmoy_plus) and np.isnan(vmoy_minus):
            continue

        for ngate in range(r_step, maxrange):
            if flag_vel[nbeam, ngate] != 0:
                continue
            current_vel = velocity[nbeam, ngate]

            if vmoy >= 0:
                decision = take_decision(vmoy_plus, current_vel, vnyq, alpha=alpha)
                vtrue = unfold(vmoy_plus, current_vel, vnyq)
            else:
                decision = take_decision(vmoy_minus, current_vel, vnyq, alpha=alpha)
                vtrue = unfold(vmoy_minus, current_vel, vnyq)

            if decision == 1:
                final_vel[nbeam, ngate] = current_vel
                flag_vel[nbeam, ngate] = 1
            elif decision == 2:
                final_vel[nbeam, ngate] = vtrue
                flag_vel[nbeam, ngate] = 2

    return final_vel, flag_vel


@jit(nopython=True)
def correct_closest_reference(azimuth, vel, final_vel, flag_vel, vnyq, alpha=0.8):
    """
    Dealias using the closest cluster of value already processed. Once the
    closest correct value is found, a take a window of 10 radials and 40 gates
    around that point and use the median as of those points as a reference.
    This function will look at unprocessed velocity only.

    Parameters:
    ===========
    azi: ndarray
        Radar scan azimuth.
    vel: ndarray <azimuth, r>
        Aliased Doppler velocity field.
    final_vel: ndarray <azimuth, r>
        Dealiased Doppler velocity field.
    flag_vel: ndarray int <azimuth, range>
        Flag array -3: No data, 0: Unprocessed, 1: good as is, 2: dealiased.
    vnyq: float
        Nyquist velocity.

    Returns:
    ========
    dealias_vel: ndarray <azimuth, range>
        Dealiased velocity slice.
    flag_vel: ndarray int <azimuth, range>
        Flag array -3: No data, 0: Unprocessed, 1: good as is, 2: dealiased.
    """
    window_azi = 10
    window_gate = 40
    maxazi, maxrange = final_vel.shape

    for nbeam in range(maxazi):
        posazi_good, posgate_good = np.where(flag_vel > 0)
        for ngate in range(0, maxrange):
            if flag_vel[nbeam, ngate] != 0:
                continue

            vel1 = vel[nbeam, ngate]

            distance = (posazi_good - nbeam) ** 2 + (posgate_good - ngate) ** 2
            if len(distance) == 0:
                continue

            closest = np.argmin(distance)
            nbeam_close = posazi_good[closest]
            ngate_close = posgate_good[closest]

            iter_azi = get_iter_pos(azimuth, nbeam_close - window_azi // 2, window_azi)
            iter_range = get_iter_range(ngate_close, window_gate, maxrange)

            vel_ref_vec = np.zeros((len(iter_azi) * len(iter_range), ), dtype=float64) + np.NaN

            # Numba doesn't support 2D slice, that's why I loop over things.
            pos = -1
            for na in iter_azi:
                pos += 1
                vel_ref_vec[pos] = np.nanmean(final_vel[na, iter_range[0]: iter_range[-1]][flag_vel[na, iter_range[0]: iter_range[-1]] > 0])
            velref = np.nanmedian(vel_ref_vec)

            decision = take_decision(velref, vel1, vnyq, alpha=alpha)

            if decision == 1:
                final_vel[nbeam, ngate] = vel1
                flag_vel[nbeam, ngate] = 1
                continue
            elif decision == 2:
                vtrue = unfold(velref, vel1, vnyq)
                if is_good_velocity(velref, vtrue, vnyq, alpha=alpha):
                    final_vel[nbeam, ngate] = vtrue
                    flag_vel[nbeam, ngate] = 2

    return final_vel, flag_vel


@jit(nopython=True)
def correct_box(azi, vel, final_vel, flag_vel, vnyq, window_range=20,
                window_azimuth=10, strategy='surround', alpha=0.8):
    """
    This module dealiases velocities based on the median of an area of corrected
    velocities preceding the gate being processed. This module is similar to
    the dealiasing technique from Bergen et al. (1988).

    Parameters:
    ===========
    azi: ndarray
        Radar scan azimuth.
    vel: ndarray <azimuth, r>
        Aliased Doppler velocity field.
    final_vel: ndarray <azimuth, r>
        Dealiased Doppler velocity field.
    flag_vel: ndarray int <azimuth, range>
        Flag array -3: No data, 0: Unprocessed, 1: good as is, 2: dealiased.
    vnyq: float
        Nyquist velocity.

    Returns:
    ========
    dealias_vel: ndarray <azimuth, range>
        Dealiased velocity slice.
    flag_vel: ndarray int <azimuth, range>
        Flag array -3: No data, 0: Unprocessed, 1: good as is, 2: dealiased.
    """
    if strategy == 'vertex':
        azi_window_offset = window_azimuth
    else:
        azi_window_offset = window_azimuth // 2

    maxazi, maxrange = final_vel.shape
    for nbeam in np.arange(maxazi - 1, -1, -1):
        for ngate in np.arange(maxrange - 1, -1, -1):
            if flag_vel[nbeam, ngate] != 0:
                continue

            myvel = vel[nbeam, ngate]

            npos_azi = get_iter_pos(azi, nbeam - azi_window_offset, window_azimuth)
            npos_range = get_iter_range(ngate, window_range, maxrange)

            flag_ref_vec = np.zeros((len(npos_range) * len(npos_azi))) + np.NaN
            vel_ref_vec = np.zeros((len(npos_range) * len(npos_azi))) + np.NaN

            # I know a slice would be better, but this is for jit to work.
            cnt = -1
            for na in npos_azi:
                for nr in npos_range:
                    cnt += 1
                    if (na, nr) == (nbeam, ngate):
                        continue
                    vel_ref_vec[cnt] = final_vel[na, nr]
                    flag_ref_vec[cnt] = flag_vel[na, nr]

            if np.sum(flag_ref_vec >= 1) == 0:
                continue

            mean_vel_ref = np.nanmean(vel_ref_vec[flag_ref_vec >= 1])

            decision = take_decision(mean_vel_ref, myvel, vnyq, alpha=alpha)

            if decision <= 0:
                continue

            if decision == 1:
                final_vel[nbeam, ngate] = myvel
                flag_vel[nbeam, ngate] = 1
            elif decision == 2:
                vtrue = unfold(mean_vel_ref, myvel, vnyq)
                if is_good_velocity(mean_vel_ref, vtrue, vnyq, alpha=alpha):
                    final_vel[nbeam, ngate] = vtrue
                    flag_vel[nbeam, ngate] = 2

    return final_vel, flag_vel


@jit(nopython=True)
def box_check(azi, final_vel, flag_vel, vnyq, window_range=80,
              window_azimuth=20, strategy='surround', alpha=0.8):
    """
    Check if all individual points are consistent with their surrounding
    velocities based on the median of an area of corrected velocities preceding
    the gate being processed. This module is similar to the dealiasing technique
    from Bergen et al. (1988). This function will look at ALL points.

    Parameters:
    ===========
    azi: ndarray
        Radar scan azimuth.
    final_vel: ndarray <azimuth, r>
        Dealiased Doppler velocity field.
    flag_vel: ndarray int <azimuth, range>
        Flag array -3: No data, 0: Unprocessed, 1: good as is, 2: dealiased.
    vnyq: float
        Nyquist velocity.

    Returns:
    ========
    dealias_vel: ndarray <azimuth, range>
        Dealiased velocity slice.
    flag_vel: ndarray int <azimuth, range>
        Flag array NEW value: 3->had to be corrected.
    """
    if strategy == 'vertex':
        azi_window_offset = window_azimuth
    else:
        azi_window_offset = window_azimuth // 2

    maxazi, maxrange = final_vel.shape
    for nbeam in range(maxazi):
        for ngate in np.arange(maxrange - 1, -1, -1):
            if flag_vel[nbeam, ngate] <= 0:
                continue

            myvel = final_vel[nbeam, ngate]

            npos_azi = get_iter_pos(azi, nbeam - azi_window_offset, window_azimuth)
            npos_range = get_iter_range(ngate, window_range, maxrange)

            flag_ref_vec = np.zeros((len(npos_range) * len(npos_azi))) + np.NaN
            vel_ref_vec = np.zeros((len(npos_range) * len(npos_azi))) + np.NaN

            cnt = -1
            for na in npos_azi:
                for nr in npos_range:
                    cnt += 1
                    if (na, nr) == (nbeam, ngate):
                        continue
                    vel_ref_vec[cnt] = final_vel[na, nr]
                    flag_ref_vec[cnt] = flag_vel[na, nr]

            if np.sum(flag_ref_vec >= 1) == 0:
                continue

            true_vel = vel_ref_vec[flag_ref_vec >= 1]
            mvel = np.nanmean(true_vel)
            svel = np.nanstd(true_vel)
            myvelref = np.nanmedian(true_vel[(true_vel >= mvel - svel) & (true_vel <= mvel + svel)])

            if not is_good_velocity(myvelref, myvel, vnyq, alpha=alpha):
                final_vel[nbeam, ngate] = myvelref
                flag_vel[nbeam, ngate] = 3

    return final_vel, flag_vel


@jit(nopython=True)
def radial_least_square_check(r, azi, vel, final_vel, flag_vel, vnyq, alpha=0.8):
    """
    Dealias a linear regression of gates inside each radials.
    This function will look at PROCESSED velocity only. This function cannot be
    fully JITed due to the use of the scipy function linregress.

    Parameters:
    ===========
    r: ndarray
        Radar range
    azi: ndarray
        Radar scan azimuth.
    vel: ndarray <azimuth, r>
        Aliased Doppler velocity field.
    final_vel: ndarray <azimuth, r>
        Dealiased Doppler velocity field.
    flag_vel: ndarray int <azimuth, range>
        Flag array -3: No data, 0: Unprocessed, 1: good as is, 2: dealiased.
    vnyq: float
        Nyquist velocity.

    Returns:
    ========
    dealias_vel: ndarray <azimuth, range>
        Dealiased velocity slice.
    flag_vel: ndarray int <azimuth, range>
        Flag array -3: No data, 0: Unprocessed, 1: good as is, 2: dealiased.
    """
    maxazi, maxrange = final_vel.shape
    myvel = np.zeros(maxrange, dtype=float64)

    for nbeam in range(maxazi):
        myvel = final_vel[nbeam, :]
        myvel[flag_vel[nbeam, :] <= 0] = np.NaN
        if len(myvel[~np.isnan(myvel)]) < 2:
            continue

        slope, intercept = linregress(r[~np.isnan(myvel)], myvel[~np.isnan(myvel)])

        fmin = intercept + slope * r - 0.4 * vnyq
        fmax = intercept + slope * r + 0.4 * vnyq
        vaffine = intercept + slope * r

        for ngate in range(maxrange):
            if flag_vel[nbeam, ngate] <= 0:
                continue

            myvel = final_vel[nbeam, ngate]

            if myvel >= fmin[ngate] and myvel <= fmax[ngate]:
                continue

            mean_vel_ref = vaffine[ngate]
            decision = take_decision(mean_vel_ref, myvel, vnyq, alpha=alpha)

            if decision <= 0:
                continue

            if decision == 1:
                final_vel[nbeam, ngate] = myvel
                flag_vel[nbeam, ngate] = 1
            elif decision == 2:
                myvel = vel[nbeam, ngate]
                vtrue = unfold(mean_vel_ref, myvel, vnyq)
                if is_good_velocity(mean_vel_ref, vtrue, vnyq, alpha=alpha):
                    final_vel[nbeam, ngate] = vtrue
                    flag_vel[nbeam, ngate] = 2

    return final_vel, flag_vel


@jit(nopython=True)
def least_square_radial_last_module(r, azi, final_vel, vnyq, alpha=0.8):
    """
    Similar as radial_least_square_check.
    """
    maxazi, maxrange = final_vel.shape
    myvel = np.zeros(maxrange, dtype=float64)

    for nbeam in range(maxazi):
        myvel = final_vel[nbeam, :]
        if len(myvel[~np.isnan(myvel)]) < 10:
            continue

        slope, intercept = linregress(r[~np.isnan(myvel)], myvel[~np.isnan(myvel)])

        fmin = intercept + slope * r - 0.4 * vnyq
        fmax = intercept + slope * r + 0.4 * vnyq
        vaffine = intercept + slope * r

        for ngate in range(maxrange):
            myvel = final_vel[nbeam, ngate]
            if np.isnan(myvel):
                continue

            if myvel >= fmin[ngate] and myvel <= fmax[ngate]:
                continue

            mean_vel_ref = vaffine[ngate]
            decision = take_decision(mean_vel_ref, myvel, vnyq, alpha=alpha)

            if decision <= 0:
                continue

            if decision == 1:
                final_vel[nbeam, ngate] = myvel
            elif decision == 2:
                vtrue = unfold(mean_vel_ref, myvel, vnyq)
                if is_good_velocity(mean_vel_ref, vtrue, vnyq, alpha=alpha):
                    final_vel[nbeam, ngate] = vtrue

    return final_vel


@jit(nopython=True)
def unfolding_3D(r, elev_down, azi_down, elev_slice, azi_slice, vel_down, flag_down,
                 velocity_slice, flag_slice, original_velocity, vnyq,
                 window_azi=20, window_range=80, alpha=0.8):
    """
    Dealias using 3D continuity. This function will look at the velocities from
    one sweep (the reference) to the other (the slice).
    Parameters:
    ===========
    r: ndarray
        Radar range
    elev_down: float
        Elevation angle of the reference sweep.
    azi_down: ndarray
        Azimuth of the reference sweep.
    elev_slice: float
        Elevation angle of the sweep to dealias.
    azi_slice: ndarray
        Azimuth of the sweep to dealias.
    vel_down: ndarray <azimuth, r>
        Velocity of the reference sweep.
    flag_down:
        Flag array of the reference
    velocity_slice: ndarray <azimuth, r>
        Velocity of the sweep to dealias.
    flag_slice:
        Flag array of the sweep to dealias.
    vnyq: float
        Nyquist velocity.
    window_azi: int
        Window size in the azimuth direction
    window_range: int
        Window size in the range direction.

    Returns:
    ========
    velocity_slice: ndarray <azimuth, range>
        Dealiased velocity slice.
    flag_slice: ndarray int <azimuth, range>
        Flag array -3: No data, 0: Unprocessed, 1: good as is, 2: dealiased.
    vel_used_as_ref: ndarray <azimuth, range>
        Velocity field used as reference (debugging purposes only).
    processing_flag: ndarray <azimuth, range>
        Flag array that track the decisions made by the algorithm (debugging
        purposes only).
    """
    vel_used_as_ref = np.zeros(velocity_slice.shape)
    processing_flag = np.zeros(velocity_slice.shape) - 3
    maxazi, maxrange = velocity_slice.shape

    r_down = r * np.cos(elev_down * np.pi / 180)
    r_slice = r * np.cos(elev_slice * np.pi / 180)

    for nbeam in range(maxazi):
        for ngate in range(maxrange):
            if flag_slice[nbeam, ngate] == -3:
                # No data here.
                processing_flag[nbeam, ngate] = -2
                continue

            current_vel = velocity_slice[nbeam, ngate]

            rpos_reference = np.argmin(np.abs(r_down - r_slice[ngate]))
            apos_reference = np.argmin(np.abs(azi_down - azi_slice[nbeam]))

            apos_iter = get_iter_pos(azi_down, apos_reference - window_azi // 2, window_azi)
            rpos_iter = get_iter_range(rpos_reference, window_range, maxrange)

            velocity_refcomp_array = np.zeros((len(rpos_iter) * len(apos_iter))) + np.NaN
            flag_refcomp_array = np.zeros((len(rpos_iter) * len(apos_iter))) - 3

            cnt = -1
            for na in apos_iter:
                for nr in rpos_iter:
                    cnt += 1
                    velocity_refcomp_array[cnt] = vel_down[na, nr]
                    flag_refcomp_array[cnt] = flag_down[na, nr]

            if np.sum(flag_refcomp_array != -3) < 1:
                # No comparison possible all gates in the reference are missing.
                processing_flag[nbeam, ngate] = -1
                continue

            compare_vel = np.nanmedian(velocity_refcomp_array[(flag_refcomp_array >= 1)])
            vel_used_as_ref[nbeam, ngate] = compare_vel

            if is_good_velocity(compare_vel, current_vel, vnyq, alpha=alpha):
                processing_flag[nbeam, ngate] = 0
                # The current velocity is in agreement with the lower tilt velocity.
                continue

            ogvel = original_velocity[nbeam, ngate]
            if is_good_velocity(compare_vel, ogvel, vnyq, alpha=alpha):
                # The original velocity was good
                velocity_slice[nbeam, ngate] = ogvel
                flag_slice[nbeam, ngate] = 1
                processing_flag[nbeam, ngate] = 1
            else:
                vtrue = unfold(compare_vel, ogvel, vnyq)
                if is_good_velocity(compare_vel, vtrue, vnyq, alpha=alpha):
                    # New dealiased velocity value found
                    velocity_slice[nbeam, ngate] = vtrue
                    flag_slice[nbeam, ngate] = 2
                    processing_flag[nbeam, ngate] = 2

    return velocity_slice, flag_slice, vel_used_as_ref, processing_flag
