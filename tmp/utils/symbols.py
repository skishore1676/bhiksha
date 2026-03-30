"""
symbols.py

Helpers to normalize and format instrument symbols.
"""
from typing import Optional
import re

def normalize_option_symbol(symbol: Optional[str]) -> str:
    """
    Convert portfolio-style option symbols (e.g., 'IWM250829P00220000-OPTION')
    to API-ready OCC symbols without the '-OPTION' suffix and uppercase.
    """
    s = (symbol or "").strip().upper()
    if s.endswith("-OPTION"):
        s = s[:-7]
    return s

def normalize_equity_symbol(symbol: Optional[str]) -> str:
    return (symbol or "").strip().upper()

def ensure_api_symbol(symbol: Optional[str], instrument_type: str) -> str:
    t = (instrument_type or "").strip().upper()
    if t == "OPTION":
        return normalize_option_symbol(symbol)
    return normalize_equity_symbol(symbol)

_OCC_WITH_SUFFIX = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}(?:-OPTION)?$")

def is_occ_option_symbol(symbol: Optional[str]) -> bool:
    """
    Returns True if the provided symbol looks like an OCC option symbol
    (optionally with '-OPTION' suffix), else False.
    """
    s = (symbol or "").strip().upper()
    return bool(_OCC_WITH_SUFFIX.match(s))

def format_occ_pretty(symbol: Optional[str]) -> str:
    """
    Convert OCC option symbol to a human-friendly form: 'IWM 25/08/29 P 220'.
    If input is not OCC-like, return uppercased input.
    """
    s = normalize_option_symbol(symbol)
    if not is_occ_option_symbol(s):
        return (s or "").upper()
    try:
        # Underlying (1-6 letters), YYMMDD (6), type (1), strike (8)
        # Find boundary between underlying and date by counting from end
        date_type_strike = s[-15:]  # 6+1+8
        base = s[:-15]
        yy = date_type_strike[0:2]
        mm = date_type_strike[2:4]
        dd = date_type_strike[4:6]
        opt_type = date_type_strike[6]
        strike_raw = date_type_strike[7:]
        strike_val = int(strike_raw) / 1000.0
        if abs(strike_val - round(strike_val)) < 1e-9:
            strike_str = str(int(round(strike_val)))
        else:
            strike_str = ("%g" % strike_val)
        return f"{base} {yy}/{mm}/{dd} {opt_type} {strike_str}"
    except Exception:
        return (s or "").upper()