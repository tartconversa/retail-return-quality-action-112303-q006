"""Standard-library statistics for exposure-adjusted screening.

Only what the application needs is implemented: incomplete gamma / Poisson /
chi-square tails, exact Poisson intervals, Benjamini-Hochberg adjustment and
two additive decompositions used to separate mix from rate effects.
"""

from __future__ import annotations

import math

_EPS = 3.0e-12
_FPMIN = 1.0e-300
_MAX_ITER = 2000


def _gser(a: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    ap = a
    total = 1.0 / a
    delta = total
    for _ in range(_MAX_ITER):
        ap += 1.0
        delta *= x / ap
        total += delta
        if abs(delta) < abs(total) * _EPS:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gcf(a: float, x: float) -> float:
    b = x + 1.0 - a
    c = 1.0 / _FPMIN
    d = 1.0 / b if abs(b) > _FPMIN else 1.0 / _FPMIN
    h = d
    for i in range(1, _MAX_ITER):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = b + an / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def gammp(a: float, x: float) -> float:
    """Regularized lower incomplete gamma P(a, x)."""
    if a <= 0.0:
        raise ValueError("a must be positive")
    if x < 0.0:
        raise ValueError("x must be non-negative")
    if x == 0.0:
        return 0.0
    return _gser(a, x) if x < a + 1.0 else 1.0 - _gcf(a, x)


def gammq(a: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(a, x)."""
    if a <= 0.0:
        raise ValueError("a must be positive")
    if x < 0.0:
        raise ValueError("x must be non-negative")
    if x == 0.0:
        return 1.0
    return 1.0 - _gser(a, x) if x < a + 1.0 else _gcf(a, x)


def chi2_sf(x: float, df: int) -> float:
    """Survival function P(X > x) of the chi-square distribution."""
    if df <= 0:
        raise ValueError("df must be positive")
    if x <= 0.0:
        return 1.0
    return min(1.0, max(0.0, gammq(df / 2.0, x / 2.0)))


def chi2_ppf(p: float, df: int) -> float:
    """Quantile x with ``chi2_sf(x, df) == p`` (bisection)."""
    if df <= 0:
        raise ValueError("df must be positive")
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    lo, hi = 0.0, max(10.0, df * 4.0)
    while chi2_sf(hi, df) > p:
        lo = hi
        hi *= 2.0
        if hi > 1.0e12:
            break
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if chi2_sf(mid, df) > p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1.0e-12 * max(1.0, hi):
            break
    return (lo + hi) / 2.0


def poisson_sf(k: int, mu: float) -> float:
    """P(X >= k) for X ~ Poisson(mu)."""
    if k <= 0:
        return 1.0
    if mu <= 0.0:
        return 0.0
    return min(1.0, max(0.0, gammp(float(k), float(mu))))


def poisson_cdf(k: int, mu: float) -> float:
    """P(X <= k) for X ~ Poisson(mu)."""
    if k < 0:
        return 0.0
    return 1.0 - poisson_sf(k + 1, mu)


def poisson_count_ci(k: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact (Garwood) confidence interval for a Poisson mean."""
    if k < 0:
        raise ValueError("k must be non-negative")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    # ``chi2_ppf(p)`` returns the quantile whose *survival* probability is p.
    lower = 0.0 if k == 0 else chi2_ppf(1.0 - alpha / 2.0, 2 * k) / 2.0
    upper = chi2_ppf(alpha / 2.0, 2 * (k + 1)) / 2.0
    return lower, upper


def poisson_rate_ci(k: int, exposure: float, alpha: float = 0.05) -> tuple[float, float, float]:
    """Exact CI for a rate, returning ``(rate, lower, upper)``."""
    if exposure <= 0:
        raise ValueError("exposure must be positive")
    lo, hi = poisson_count_ci(k, alpha)
    return k / exposure, lo / exposure, hi / exposure


def rate_ratio_ci(k1: int, n1: float, k0: int, n0: float, alpha: float = 0.05) -> tuple[float, float, float, float]:
    """Compare two rates with a conditional exact (binomial) test.

    Under the null hypothesis of equal rates, the number of events in the
    follow-up period is Binomial(total events, n1/(n1+n0)).  The returned
    interval is obtained by mapping the Clopper-Pearson interval for that
    proportion back to a rate ratio, so the interval and p-value are
    consistent even when one period has zero events.

    Returns ``(ratio, lower, upper, p_value)``.
    """
    if n1 <= 0 or n0 <= 0:
        raise ValueError("exposures must be positive")
    total = k1 + k0
    if total == 0:
        return 0.0, 0.0, float("inf"), 1.0
    share = n1 / (n1 + n0)
    p_value = binomial_test(k1, total, share)
    lo_p, hi_p = binomial_exact_ci(k1, total, alpha)
    def to_ratio(p: float) -> float:
        if p <= 0.0:
            return 0.0
        if p >= 1.0:
            return float("inf")
        return (p / (1.0 - p)) * (n0 / n1)
    return to_ratio(k1 / total), to_ratio(lo_p), to_ratio(hi_p), p_value


def _z_from_alpha(alpha: float) -> float:
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    target = 1.0 - alpha / 2.0
    lo, hi = 0.0, 8.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        cdf = 0.5 * math.erfc(-mid / math.sqrt(2.0))
        if cdf < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Lentz)."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _FPMIN:
        d = _FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, _MAX_ITER):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return h


def betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b)."""
    if a <= 0 or b <= 0:
        raise ValueError("a and b must be positive")
    if x < 0.0 or x > 1.0:
        raise ValueError("x must be in [0, 1]")
    if x == 0.0:
        return 0.0
    if x == 1.0:
        return 1.0
    ln_bt = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    bt = math.exp(ln_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def beta_ppf(q: float, a: float, b: float) -> float:
    """Quantile of the Beta(a, b) distribution (bisection)."""
    if not 0.0 < q < 1.0:
        raise ValueError("q must be in (0, 1)")
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if betai(a, b, mid) < q:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1.0e-14:
            break
    return (lo + hi) / 2.0


def binomial_exact_ci(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Clopper-Pearson exact confidence interval for a binomial proportion."""
    if n <= 0:
        raise ValueError("n must be positive")
    if k < 0 or k > n:
        raise ValueError("k must be between 0 and n")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    lower = 0.0 if k == 0 else beta_ppf(alpha / 2.0, k, n - k + 1)
    upper = 1.0 if k == n else beta_ppf(1.0 - alpha / 2.0, k + 1, n - k)
    return lower, upper


def _binom_pmf(k: int, n: int, p: float) -> float:
    if k < 0 or k > n:
        return 0.0
    if p <= 0.0:
        return 1.0 if k == 0 else 0.0
    if p >= 1.0:
        return 1.0 if k == n else 0.0
    log_pmf = (
        math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
        + k * math.log(p) + (n - k) * math.log1p(-p)
    )
    return min(1.0, math.exp(log_pmf))


def binomial_test(k: int, n: int, p: float = 0.5) -> float:
    """Two-sided exact binomial test p-value (method of small p-values).

    More conservative than doubling one tail for asymmetric distributions,
    and safe for the very small counts seen in this application.
    """
    if n <= 0:
        return 1.0
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    observed = _binom_pmf(k, n, p)
    tolerance = observed * (1.0 + 1.0e-9)
    total = sum(_binom_pmf(i, n, p) for i in range(n + 1) if _binom_pmf(i, n, p) <= tolerance)
    return min(1.0, total)


def bh_qvalues(pvalues: list[float]) -> list[float]:
    """Benjamini-Hochberg false-discovery-rate adjusted values."""
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    q = [1.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        idx = order[rank - 1]
        running = min(running, pvalues[idx] * m / rank)
        q[idx] = min(1.0, max(0.0, running))
    return q


def decompose_rate_change(
    early: dict[str, tuple[float, float]],
    late: dict[str, tuple[float, float]],
) -> dict[str, float]:
    """Split a crude-rate change into mix and within-stratum rate effects.

    ``early``/``late`` map stratum -> ``(exposure, events)``.  A stratum that
    exists in only one period is assigned that period's pooled rate (direct
    standardisation approximation).  ``delta == mix + rate + residual`` where
    ``residual`` is the mix/rate interaction term (zero only when strata share
    a common rate).
    """
    def pooled(rows: dict[str, tuple[float, float]]) -> tuple[float, float]:
        return sum(v[0] for v in rows.values()), sum(v[1] for v in rows.values())

    n_e, k_e = pooled(early)
    n_l, k_l = pooled(late)
    if n_e <= 0 or n_l <= 0:
        raise ValueError("both periods need positive exposure")
    r_e = k_e / n_e
    r_l = k_l / n_l
    strata = sorted(set(early) | set(late))
    w_e = {s: early.get(s, (0.0, 0.0))[0] / n_e for s in strata}
    w_l = {s: late.get(s, (0.0, 0.0))[0] / n_l for s in strata}
    rate_e = {}
    rate_l = {}
    for s in strata:
        ne, ke = early.get(s, (0.0, 0.0))
        nl, kl = late.get(s, (0.0, 0.0))
        rate_e[s] = ke / ne if ne > 0 else r_e
        rate_l[s] = kl / nl if nl > 0 else r_l
    mix = sum((w_l[s] - w_e[s]) * rate_e[s] for s in strata)
    rate = sum(w_l[s] * (rate_l[s] - rate_e[s]) for s in strata)
    delta = r_l - r_e
    return {
        "early_exposure": n_e,
        "early_events": k_e,
        "early_rate": r_e,
        "late_exposure": n_l,
        "late_events": k_l,
        "late_rate": r_l,
        "delta": delta,
        "mix_effect": mix,
        "rate_effect": rate,
        "residual": delta - mix - rate,
    }


def decompose_entity_mix(
    entity: dict[str, tuple[float, float]],
    background: dict[str, tuple[float, float]],
) -> dict[str, float]:
    """Compare an entity's crude rate with its strata-adjusted expectation.

    ``entity``/``background`` map stratum -> ``(exposure, events)``.  The
    background rates are the reference; ``mix_effect`` is the share explained
    purely by the entity's channel/policy composition and ``residual`` is the
    interaction term.  Identity is preserved:
    ``difference == mix_effect + rate_effect + residual``.
    """
    n_e = sum(v[0] for v in entity.values())
    k_e = sum(v[1] for v in entity.values())
    n_b = sum(v[0] for v in background.values())
    k_b = sum(v[1] for v in background.values())
    if n_e <= 0 or n_b <= 0:
        raise ValueError("positive exposure required")
    r_e = k_e / n_e
    r_b = k_b / n_b
    strata = sorted(set(entity) | set(background))
    mix = 0.0
    rate_part = 0.0
    for s in strata:
        ne, ke = entity.get(s, (0.0, 0.0))
        nb, kb = background.get(s, (0.0, 0.0))
        bg_rate = kb / nb if nb > 0 else r_b
        if ne <= 0:
            continue
        entity_rate = ke / ne
        mix += (ne / n_e - (nb / n_b if n_b else 0.0)) * bg_rate
        rate_part += (ne / n_e) * (entity_rate - bg_rate)
    diff = r_e - r_b
    return {
        "entity_rate": r_e,
        "background_rate": r_b,
        "difference": diff,
        "mix_effect": mix,
        "rate_effect": rate_part,
        "residual": diff - mix - rate_part,
    }
