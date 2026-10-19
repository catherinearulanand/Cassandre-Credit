"""Synthetic obligors with valid-format, checksum-correct identifiers and fictitious names."""

from __future__ import annotations

import random
import string
from dataclasses import dataclass

from domain.entities import Obligor
from identifiers.validators import gstin_check_char, lei_check_digits

_PREFIX = [
    "Saffron",
    "Indigo",
    "Monsoon",
    "Teakwood",
    "Coral",
    "Vermilion",
    "Peacock",
    "Lotus",
    "Banyan",
    "Marigold",
    "Sandal",
    "Jade",
    "Turmeric",
    "Cardamom",
    "Kesar",
    "Neem",
]
_SECTOR = [
    ("Textiles", "13111"),
    ("Polymers", "22209"),
    ("Engineering", "28199"),
    ("Agro Foods", "10611"),
    ("Pharma", "21002"),
    ("Logistics", "49231"),
    ("Steel Tubes", "24106"),
    ("Auto Components", "29301"),
    ("Ceramics", "23933"),
]
_STATES = [
    ("27", "MH", "WEST"),
    ("24", "GJ", "WEST"),
    ("29", "KA", "SOUTH"),
    ("33", "TN", "SOUTH"),
    ("07", "DL", "NORTH"),
    ("09", "UP", "NORTH"),
    ("19", "WB", "EAST"),
    ("36", "TS", "SOUTH"),
]
_FIRST = ["Aarav", "Diya", "Kabir", "Meher", "Vihaan", "Ira", "Reyansh", "Anaya", "Advait", "Myra"]
_LAST = ["Synthwala", "Fictiwal", "Demoraj", "Testkar", "Mockrao", "Samplekar"]
PORTFOLIOS = ("MIDCORP", "LARGECORP", "SME")


@dataclass
class ObligorBundle:
    obligor: Obligor
    trajectory: str  # healthy | deteriorating | shock
    shock_quarter: int | None
    state_code: str


def _pan(rng: random.Random, name_initial: str) -> str:
    letters = "".join(rng.choice(string.ascii_uppercase) for _ in range(3))
    return f"{letters}C{name_initial}{rng.randint(1000, 9999)}{rng.choice(string.ascii_uppercase)}"


def make_obligor(rng: random.Random, idx: int, trajectory: str | None = None) -> ObligorBundle:
    prefix = rng.choice(_PREFIX)
    sector, nic = rng.choice(_SECTOR)
    name = f"{prefix} Synthetic {sector} {rng.choice(['Pvt Ltd', 'Ltd'])}"
    state_code, state_abbr, region = rng.choice(_STATES)
    pan = _pan(rng, prefix[0])
    body = f"{state_code}{pan}1Z"
    gstin = body + gstin_check_char(body)
    lei_body = "335800" + "".join(
        rng.choice(string.digits + string.ascii_uppercase) for _ in range(12)
    )
    lei = lei_body + lei_check_digits(lei_body)
    year = rng.randint(1985, 2015)
    cin = f"{'U' if 'Pvt' in name else 'L'}{nic}{state_abbr}{year}{'PTC' if 'Pvt' in name else 'PLC'}{rng.randint(100000, 999999)}"
    traj = (
        trajectory or rng.choices(["healthy", "deteriorating", "shock"], weights=[0.7, 0.2, 0.1])[0]
    )
    promoters = [f"{rng.choice(_FIRST)} {rng.choice(_LAST)}" for _ in range(rng.randint(1, 3))]
    obl = Obligor(
        obligor_id=f"OBL-{idx:06d}",
        cif=f"CIF{idx:07d}",
        legal_name=name,
        pan=pan,
        cin=cin,
        lei=lei,
        gstins=[gstin],
        nic_code=nic,
        region=region,
        portfolio=rng.choice(PORTFOLIOS),
        internal_rating=rng.choice(["CR2", "CR3", "CR3", "CR4", "CR4", "CR5"]),
        promoter_names=promoters,
    )
    return ObligorBundle(obl, traj, rng.randint(6, 11) if traj == "shock" else None, state_code)
