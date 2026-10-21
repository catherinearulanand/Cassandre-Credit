"""Entity resolution: which obligor, group or related party is this name and identifier?

Identifiers decide; names only suggest. A PAN, CIN, LEI or GSTIN match is certain (and checked
against the others for contradictions). A fuzzy name match is a *candidate* that a human must
confirm, and is never merged automatically. A contradiction between identifiers is surfaced as a
conflict and blocks any downstream use until a human resolves it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from domain.entities import Obligor
from identifiers.validators import is_valid_cin, is_valid_gstin, is_valid_lei, is_valid_pan
from rapidfuzz import fuzz

_SUFFIX = re.compile(
    r"\b(private|pvt|public|limited|ltd|llp|co|company|corporation|corp|industries|india)\b"
)

Basis = Literal["pan", "cin", "lei", "gstin", "name"]


def normalise_name(name: str) -> str:
    folded = unicodedata.normalize("NFKC", name).casefold()
    kept = "".join(c if unicodedata.category(c)[0] in "LMN" else " " for c in folded)
    stripped = " ".join(_SUFFIX.sub(" ", kept).split())
    # a name made only of suffix words ("India Industries Limited") is compared as written
    return stripped or " ".join(kept.split())


def _initials_match(short: str, full: str) -> bool:
    """ "r k iyer" matches "ramesh kumar iyer": single-letter tokens stand for name starts."""
    a, b = short.split(), full.split()
    if len(a) != len(b) or not a or a[-1] != b[-1]:
        return False
    return all(x == y or (len(x) == 1 and y.startswith(x)) for x, y in zip(a, b, strict=True))


@dataclass(frozen=True)
class Query:
    name: str | None = None
    pan: str | None = None
    cin: str | None = None
    lei: str | None = None
    gstin: str | None = None


@dataclass(frozen=True)
class Match:
    obligor_id: str
    basis: Basis
    score: int  # whole percent; 100 for identifier matches
    certain: bool


@dataclass(frozen=True)
class Resolution:
    status: Literal["certain", "candidates", "conflict", "none", "invalid_identifier"]
    matches: tuple[Match, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def needs_human(self) -> bool:
        return self.status in ("candidates", "conflict", "invalid_identifier")


@dataclass
class EntityIndex:
    name_threshold: int = 90
    obligors: dict[str, Obligor] = field(default_factory=dict)
    _by_pan: dict[str, str] = field(default_factory=dict)
    _by_cin: dict[str, str] = field(default_factory=dict)
    _by_lei: dict[str, str] = field(default_factory=dict)
    _by_gstin: dict[str, str] = field(default_factory=dict)

    def add(self, o: Obligor) -> None:
        """Validate and check every identifier first, then write: a failure leaves no trace."""
        for label, val, ok in (
            ("PAN", o.pan, is_valid_pan),
            ("CIN", o.cin, is_valid_cin),
            ("LEI", o.lei, is_valid_lei),
        ):
            if val and not ok(val):
                raise ValueError(f"{label} {val!r} is malformed")
        for g in o.gstins:
            if not is_valid_gstin(g):
                raise ValueError(f"GSTIN {g!r} is malformed")
            if g[2:12] != o.pan:
                raise ValueError(f"GSTIN {g} does not embed the obligor's PAN")
        for table, key, label in (
            (self._by_pan, o.pan, "PAN"),
            (self._by_cin, o.cin, "CIN"),
            (self._by_lei, o.lei, "LEI"),
        ):
            other = table.get(key) if key else None
            if other is not None and other != o.obligor_id:
                raise ValueError(f"{label} {key} already belongs to {other}")
        for g in o.gstins:
            other = self._by_gstin.get(g)
            if other is not None and other != o.obligor_id:
                raise ValueError(f"GSTIN {g} already belongs to {other}")
        for table in (self._by_pan, self._by_cin, self._by_lei, self._by_gstin):
            for k in [k for k, v in table.items() if v == o.obligor_id]:
                del table[k]  # a re-add replaces the old identifiers
        for table, key in ((self._by_pan, o.pan), (self._by_cin, o.cin), (self._by_lei, o.lei)):
            if key:
                table[key] = o.obligor_id
        for g in o.gstins:
            self._by_gstin[g] = o.obligor_id
        self.obligors[o.obligor_id] = o

    def resolve(self, q: Query) -> Resolution:
        notes: list[str] = []
        for label, val, ok in (
            ("PAN", q.pan, is_valid_pan),
            ("CIN", q.cin, is_valid_cin),
            ("LEI", q.lei, is_valid_lei),
            ("GSTIN", q.gstin, is_valid_gstin),
        ):
            if val is not None and not ok(val):
                return Resolution("invalid_identifier", notes=(f"{label} is malformed",))
        hits: dict[str, list[Basis]] = {}
        for basis, val, table in (
            ("pan", q.pan, self._by_pan),
            ("cin", q.cin, self._by_cin),
            ("lei", q.lei, self._by_lei),
            ("gstin", q.gstin, self._by_gstin),
        ):
            if val and val in table:
                hits.setdefault(table[val], []).append(basis)  # type: ignore[arg-type]
        if q.gstin and q.gstin[2:12] in self._by_pan and not q.pan:
            hits.setdefault(self._by_pan[q.gstin[2:12]], []).append("pan")
        # A GSTIN carries the holder's PAN: if it disagrees with the PAN supplied, that is a conflict.
        if q.gstin and q.pan and q.gstin[2:12] != q.pan:
            return Resolution(
                "conflict", notes=("GSTIN embeds a different PAN from the one supplied",)
            )
        if len(hits) > 1:
            return Resolution(
                "conflict",
                tuple(Match(o, b[0], 100, False) for o, b in sorted(hits.items())),
                ("identifiers point to different obligors",),
            )
        if len(hits) == 1:
            oid, bases = next(iter(hits.items()))
            ob = self.obligors[oid]
            for label, supplied, stored in (
                ("PAN", q.pan, ob.pan),
                ("CIN", q.cin, ob.cin),
                ("LEI", q.lei, ob.lei),
            ):
                if supplied and stored and supplied != stored:
                    return Resolution(
                        "conflict",
                        (Match(oid, bases[0], 100, False),),
                        (f"{label} supplied differs from the one on record",),
                    )
            if q.gstin and q.gstin not in ob.gstins and q.gstin[2:12] != ob.pan:
                return Resolution(
                    "conflict", (Match(oid, bases[0], 100, False),), ("GSTIN embeds another PAN",)
                )
            if (
                q.name
                and fuzz.token_set_ratio(normalise_name(q.name), normalise_name(ob.legal_name)) < 60
            ):
                notes.append("name differs from the registered name; verify")
            return Resolution("certain", (Match(oid, bases[0], 100, True),), tuple(notes))
        if q.name:
            qn = normalise_name(q.name)
            scored = sorted(
                (
                    (fuzz.token_set_ratio(qn, normalise_name(o.legal_name)), o.obligor_id)
                    for o in self.obligors.values()
                ),
                reverse=True,
            )
            cands = tuple(
                Match(oid, "name", int(s), False) for s, oid in scored if s >= self.name_threshold
            )
            if cands:
                return Resolution("candidates", cands, ("name match only; a human must confirm",))
        return Resolution("none")

    # --------------------------------------------------------------- related parties ------
    def group_members(self, obligor_id: str) -> set[str]:
        gid = self.obligors[obligor_id].group_id
        if gid is None:
            return {obligor_id}
        return {o.obligor_id for o in self.obligors.values() if o.group_id == gid}

    def related_party_names(self, obligor_id: str) -> set[str]:
        """Normalised names of group companies and promoters, for payee screening in the gate."""
        names: set[str] = set()
        for m in self.group_members(obligor_id) - {obligor_id}:
            names.add(normalise_name(self.obligors[m].legal_name))
        for p in self.obligors[obligor_id].promoter_names:
            names.add(normalise_name(p))
        return names

    def is_related_payee(self, obligor_id: str, payee_name: str, threshold: int = 88) -> bool:
        pn = normalise_name(payee_name)
        return any(
            fuzz.token_set_ratio(pn, n) >= threshold
            or _initials_match(pn, n)
            or _initials_match(n, pn)
            for n in self.related_party_names(obligor_id)
        )
