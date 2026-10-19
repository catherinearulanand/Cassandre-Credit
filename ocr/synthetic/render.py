"""Render a quarterly Schedule III-style statement page with exact ground truth.

Why synthetic: ground truth is known to the paisa, so accuracy numbers are exact rather than
estimated from human labels. What it cannot prove: behaviour on real bank scans. The same
harness runs unchanged on consented real samples once those exist (see docs/OCR_ACCURACY.md).

Degradation levels simulate scanning damage in increasing order:
  clean     rendered text, no damage
  mild      slight skew, faint noise, mild JPEG (office scanner at 200 dpi)
  moderate  skew, blur, speckle noise, strong JPEG, reduced resolution (branch MFP / fax-quality)
  severe    large skew, heavy blur and noise, low resolution, contrast loss, shadow band
"""

from __future__ import annotations

import io
import random
from dataclasses import dataclass, field
from decimal import Decimal

import numpy as np
from domain.coa import BY_CODE
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from platform_core.money import format_indian_number
from testing.synthetic.financials import UNIT_PAISE, QuarterFinancials

LEVELS = ("clean", "mild", "moderate", "severe")

PL_ORDER = [
    "revenue_from_operations",
    "other_income",
    "total_income",
    "cost_of_materials",
    "change_in_inventories",
    "employee_benefits",
    "finance_costs",
    "depreciation_amortisation",
    "other_expenses",
    "total_expenses",
    "pbt",
    "current_tax",
    "deferred_tax",
    "tax_expense",
    "pat",
]
BS_ORDER = [
    "share_capital",
    "other_equity",
    "total_equity",
    "lt_borrowings",
    "lease_liabilities",
    "deferred_tax_liability",
    "lt_provisions",
    "other_non_current_liabilities",
    "non_current_liabilities",
    "st_borrowings",
    "current_maturities_ltd",
    "trade_payables",
    "other_current_liabilities",
    "st_provisions",
    "current_liabilities",
    "total_equity_and_liabilities",
    "net_fixed_assets",
    "cwip",
    "intangible_assets",
    "lt_investments",
    "deferred_tax_asset",
    "other_non_current_assets",
    "non_current_assets",
    "inventories",
    "trade_receivables",
    "cash_and_equivalents",
    "liquid_investments",
    "other_current_assets",
    "current_assets",
    "total_assets",
]
# print labels differ by company; the extractor must map them back to chart-of-accounts codes
LABEL_OVERRIDES = {
    "total_equity": ["Total Equity", "Shareholders' Funds"],
    "cwip": ["Capital Work-in-progress"],
    "pat": ["Profit for the year", "Profit After Tax"],
    "pbt": ["Profit before tax"],
}
FONT_CANDIDATES = ["arial.ttf", "calibri.ttf", "times.ttf", "verdana.ttf", "tahoma.ttf", "cour.ttf"]


@dataclass
class GtField:
    code: str
    label_printed: str
    value_paise: int
    text_current: str
    bbox: tuple[float, float, float, float]  # normalised amount-cell box
    prior_paise: int | None = None


@dataclass
class RenderedPage:
    image: Image.Image
    fields: list[GtField]
    unit_text: str
    unit_name: str
    statement: str
    level: str
    font: str
    meta: dict[str, object] = field(default_factory=dict)


def _font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()


def _fmt(units: int, unit_name: str) -> str:
    """Print ``units`` (0.01 lakh) in the statement unit, Indian grouping, negatives in brackets."""
    rupees = Decimal(units) * 1000
    if unit_name == "lakhs":
        shown, dp = rupees / Decimal(100_000), 2
    elif unit_name == "thousands":
        shown, dp = rupees / Decimal(1_000), 0
    else:  # rupees
        shown, dp = rupees, 0
    s = format_indian_number(abs(shown))
    if dp == 0:
        s = s.rsplit(".", 1)[0]
    return f"({s})" if shown < 0 else s


def _pick_label(rng: random.Random, code: str) -> str:
    line = BY_CODE[code]
    options = LABEL_OVERRIDES.get(code) or [line.label, *[a.title() for a in line.aliases[:2]]]
    return rng.choice(options)


def render_statement(
    rng: random.Random,
    q: QuarterFinancials,
    prior: QuarterFinancials | None,
    statement: str,
    level: str,
    company: str = "Saffron Synthetic Textiles Pvt Ltd",
    unit_name: str | None = None,
) -> RenderedPage:
    unit_name = unit_name or rng.choice(["lakhs", "lakhs", "thousands", "rupees"])
    unit_text = {"lakhs": "(₹ in lakhs)", "thousands": "(₹ in '000)", "rupees": "(Amount in ₹)"}[
        unit_name
    ]
    order = PL_ORDER if statement == "PL" else BS_ORDER
    title = "Statement of Profit and Loss" if statement == "PL" else "Balance Sheet"
    font_name = rng.choice(FONT_CANDIDATES)
    size = rng.choice([22, 24, 26])
    row_h = size + 12
    width, height = 1240, 190 + row_h * (len(order) + 1) + 80
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    f_t, f_b = _font(font_name, size + 6), _font(font_name, size)
    d.text((60, 30), company, fill="black", font=f_t)
    d.text(
        (60, 70),
        f"{title} for the quarter ended {q.period_end.strftime('%d %B %Y')}",
        fill="black",
        font=f_b,
    )
    d.text((60, 105), unit_text, fill="black", font=f_b)
    d.text((820, 105), "Current", fill="black", font=f_b)
    d.text((1010, 105), "Previous", fill="black", font=f_b)
    d.line((50, 150, width - 50, 150), fill="black", width=2)
    fields: list[GtField] = []
    y = 165
    for code in order:
        is_total = BY_CODE[code].is_total
        label = _pick_label(rng, code)
        fnt = _font(font_name, size)
        d.text((60 + (0 if is_total else 24), y), label, fill="black", font=fnt)
        cur = _fmt(q.lines[code], unit_name)
        pri = _fmt(prior.lines[code], unit_name) if prior else "-"
        cw = d.textlength(cur, font=fnt)
        x1 = 960
        d.text((x1 - cw, y), cur, fill="black", font=fnt)
        d.text((1150 - d.textlength(pri, font=fnt), y), pri, fill="black", font=fnt)
        bbox = ((x1 - cw - 4) / width, (y - 2) / height, (x1 + 4) / width, (y + size + 6) / height)
        fields.append(
            GtField(
                code,
                label,
                q.lines[code] * UNIT_PAISE,
                cur,
                bbox,
                prior.lines[code] * UNIT_PAISE if prior else None,
            )
        )
        if is_total:
            d.line((560, y - 3, 960, y - 3), fill="black", width=1)
        y += row_h
    page = RenderedPage(
        img,
        fields,
        unit_text,
        unit_name,
        statement,
        level,
        font_name,
        {"size": size, "period_end": q.period_end.isoformat()},
    )
    page.image = degrade(rng, img, level)
    return page


def degrade(rng: random.Random, img: Image.Image, level: str) -> Image.Image:
    if level == "clean":
        return img
    params = {
        "mild": dict(rot=1.0, blur=0.4, noise=6, jpeg=70, scale=1.0, contrast=0.95, shadow=0),
        "moderate": dict(rot=2.0, blur=0.9, noise=14, jpeg=45, scale=0.78, contrast=0.85, shadow=0),
        "severe": dict(rot=3.5, blur=1.3, noise=24, jpeg=30, scale=0.62, contrast=0.72, shadow=40),
    }[level]
    w, h = img.size
    out = img.convert("L")
    angle = rng.uniform(-params["rot"], params["rot"])
    out = out.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=255)
    if params["scale"] < 1.0:
        out = out.resize(
            (int(w * params["scale"]), int(h * params["scale"])), Image.Resampling.BILINEAR
        )
        out = out.resize((w, h), Image.Resampling.BILINEAR)
    out = out.filter(ImageFilter.GaussianBlur(params["blur"]))
    arr = np.asarray(out, dtype=np.float32)
    arr = 255 - (255 - arr) * params["contrast"]
    nrng = np.random.default_rng(rng.randint(0, 2**31))
    arr += nrng.normal(0, params["noise"], arr.shape)
    if params["shadow"]:
        band = np.linspace(0, 1, h, dtype=np.float32)[:, None]
        arr -= params["shadow"] * np.exp(-((band - rng.uniform(0.2, 0.8)) ** 2) / 0.02)
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="JPEG", quality=params["jpeg"])
    return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
