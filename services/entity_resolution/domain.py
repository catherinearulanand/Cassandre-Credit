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
_PUNCT = re.compile(r"[^a-z0-9 ]+")

Basis = Literal["pan", "cin", "lei", "gstin", "name"]


def normalise_name(name: str) -> str:
    s = unicodedata.normalize("NFKC", name).casefold()
    s = _PUNCT.sub(" ", s)
    s = _SUFFIX.sub(" ", s)
    return " ".join(s.split())


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
        for table, key, label in (
            (self._by_pan, o.pan, "PAN"),
            (self._by_cin, o.cin, "CIN"),
            (self._by_lei, o.lei, "LEI"),
        ):
            if key:
                other = table.get(key)
                if other is not None and other != o.obligor_id:
                    raise ValueError(f"{label} {key} already belongs to {other}")
                table[key] = o.obligor_id
        for g in o.gstins:
            other = self._by_gstin.get(g)
            if other is not None and other != o.obligor_id:
                raise ValueError(f"GSTIN {g} already belongs to {other}")
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
            fuzz.token_set_ratio(pn, n) >= threshold for n in self.related_party_names(obligor_id)
        )
