# utils.py — Crack SMS v20 Professional Edition
# Utility helpers for bot core functionality
import re

def to_bold(text: str) -> str:
    """Wrap text in HTML bold tags."""
    return f"<b>{text}</b>"

def mask_number(num: str, show_last: int = 5) -> str:
    """Mask a phone number, showing only the last N digits."""
    digits = re.sub(r'[^0-9]', '', str(num))
    if len(digits) <= show_last:
        return digits
    return '•' * (len(digits) - show_last) + digits[-show_last:]

def chunk_list(lst: list, size: int):
    """Split a list into chunks of given size."""
    for i in range(0, len(lst), size):
        yield lst[i:i + size]

def safe_int(val, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default
