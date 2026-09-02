"""Bounded knapsack via binary splitting, with reconstruction."""
from __future__ import annotations


def bounded_knapsack(items: list[tuple[int, float, int]], capacity: int
                     ) -> tuple[float, list[int]]:
    """items: (size, value, max_count). Returns (best value, count taken per item).

    Bounded multiplicities are binary-split into 0/1 groups, which keeps the DP a
    single backwards pass per group and avoids the sequential dependency of the
    unbounded recurrence.
    """
    groups: list[tuple[int, float, int, int]] = []  # size, value, item index, multiple
    for idx, (size, value, count) in enumerate(items):
        if size <= 0 or size > capacity or count <= 0:
            continue
        count = min(count, capacity // size)
        k = 1
        while count > 0:
            take = min(k, count)
            groups.append((size * take, value * take, idx, take))
            count -= take
            k *= 2

    NEG = float("-inf")
    dp = [NEG] * (capacity + 1)
    dp[0] = 0.0
    # parent[c] = index into groups used to reach c, or -1
    parent = [-1] * (capacity + 1)

    for gi, (gsize, gvalue, _, _) in enumerate(groups):
        for c in range(capacity, gsize - 1, -1):
            prev = dp[c - gsize]
            if prev == NEG:
                continue
            cand = prev + gvalue
            if cand > dp[c]:
                dp[c] = cand
                parent[c] = gi

    best_c = max(range(capacity + 1), key=lambda c: dp[c])
    counts = [0] * len(items)
    c = best_c
    # walk back; each group can only be used once, so re-derive by removing it
    used = set()
    while c > 0 and parent[c] != -1:
        gi = parent[c]
        gsize, _, idx, mult = groups[gi]
        if gi in used:
            break
        used.add(gi)
        counts[idx] += mult
        c -= gsize
    return dp[best_c], counts


def np_knapsack(sizes, values, capacity: int):
    """Vectorised 0/1 knapsack. Returns (best_value, best_capacity, taken_group_indices).

    One numpy pass per group over the capacity axis. The unbounded recurrence cannot be
    vectorised this way (it needs already-updated cells), which is why callers must
    binary-split bounded multiplicities into 0/1 groups first.
    """
    import numpy as np

    n = len(sizes)
    NEG = -1e18
    dp = np.full(capacity + 1, NEG)
    dp[0] = 0.0
    took = np.zeros((n, capacity + 1), dtype=bool)

    for g in range(n):
        s = sizes[g]
        if s <= 0 or s > capacity:
            continue
        cand = np.full(capacity + 1, NEG)
        cand[s:] = dp[:capacity + 1 - s] + values[g]
        better = cand > dp
        dp = np.where(better, cand, dp)
        took[g] = better

    best_c = int(np.argmax(dp))
    chosen = []
    c = best_c
    for g in range(n - 1, -1, -1):
        if took[g, c]:
            chosen.append(g)
            c -= sizes[g]
    return float(dp[best_c]), best_c, chosen


def split_groups(items):
    """Binary-split (size, value, count) items into 0/1 groups.

    Returns (sizes, values, owner) where owner[g] is the originating item index.
    """
    sizes, values, owner = [], [], []
    for idx, (size, value, count) in enumerate(items):
        if size <= 0 or count <= 0:
            continue
        k = 1
        while count > 0:
            take = min(k, count)
            sizes.append(size * take)
            values.append(value * take)
            owner.append((idx, take))
            count -= take
            k *= 2
    return sizes, values, owner
