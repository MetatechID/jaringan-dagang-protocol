"""Canonical bank codes for payout bank accounts.

The canonical format is the BI numeric code ("014" = BCA) — the format
Sento and Dipay both use natively for disbursements. Xendit uses its own
alphabetical codes, mapped here. The payouts GET endpoint exposes
``BANKS`` as ``bank_keys`` so the seller dashboard renders one shared
bank picker instead of one per gateway.

Sources: the Dipay table mirrors the 119-bank list in the dashboard
(``DIPAY_BANK_CODES`` in seller-dashboard PayoutsTab.tsx), the Sento
subset mirrors ``SENTO_BANK_CODES`` there. Xendit mapping covers the 7
banks in the old ``BANK_CODES`` list — banks outside it are simply not
offered when the brand's payment_provider is xendit.
"""

from __future__ import annotations

from typing import Optional

PROVIDERS = ("xendit", "sento", "dipay")

# The 16-bank Sento subset (SENTO_BANK_CODES in the dashboard).
SENTO_CODES: frozenset[str] = frozenset({
    "002", "008", "009", "014", "011", "013", "016", "019",
    "022", "028", "200", "451", "153", "542", "111", "110",
})

# Xendit alphabetical codes for the banks its dashboard previously
# offered (BANK_CODES in the dashboard). Canonical BI code → Xendit code.
XENDIT_BY_CODE: dict[str, str] = {
    "014": "BCA",
    "008": "MANDIRI",
    "009": "BNI",
    "002": "BRI",
    "013": "PERMATA",
    "022": "CIMB",
    "451": "BSI",
}

# Canonical bank list — the 119-bank Dipay table (popular & digital
# banks first), which is a superset of the Sento subset.
BANKS: list[dict[str, str]] = [
    # Popular & Digital Banks
    {"code": "014", "label": "BCA"},
    {"code": "008", "label": "Bank Mandiri"},
    {"code": "009", "label": "BNI"},
    {"code": "002", "label": "BRI"},
    {"code": "013", "label": "Bank Permata"},
    {"code": "022", "label": "CIMB Niaga"},
    {"code": "451", "label": "Bank Syariah Indonesia (BSI)"},
    {"code": "011", "label": "Bank Danamon"},
    {"code": "200", "label": "BTN"},
    {"code": "028", "label": "OCBC"},
    {"code": "501", "label": "BCA Digital"},
    {"code": "542", "label": "Bank Jago"},
    {"code": "535", "label": "SeaBank (Bank Seabank Indonesia)"},
    {"code": "562", "label": "Superbank"},
    {"code": "567", "label": "Allo Bank"},
    {"code": "459", "label": "Krom Bank Indonesia"},
    {"code": "490", "label": "Bank Neo Commerce"},
    {"code": "494", "label": "Bank Raya Indonesia"},
    {"code": "947", "label": "Bank Aladin Syariah"},
    {"code": "147", "label": "Bank Muamalat Indonesia"},
    # Commercial, Regional (BPD) & Other Supported Banks
    {"code": "016", "label": "BII Maybank"},
    {"code": "019", "label": "Bank Panin"},
    {"code": "023", "label": "Bank UOB Indonesia"},
    {"code": "031", "label": "CITIBANK"},
    {"code": "032", "label": "JPMorgan Chase Bank"},
    {"code": "033", "label": "Bank of America"},
    {"code": "036", "label": "Bank China Construction Bank Indonesia"},
    {"code": "037", "label": "Bank Artha Graha Internasional"},
    {"code": "042", "label": "MUFG Bank"},
    {"code": "046", "label": "Bank DBS Indonesia"},
    {"code": "047", "label": "Bank Resona Perdania"},
    {"code": "048", "label": "Bank Mizuho Indonesia"},
    {"code": "050", "label": "Standard Chartered"},
    {"code": "054", "label": "Bank Capital Indonesia"},
    {"code": "057", "label": "Bank BNP Paribas Indonesia"},
    {"code": "061", "label": "ANZ Indonesia"},
    {"code": "067", "label": "Deutsche Bank AG"},
    {"code": "069", "label": "Bank of China"},
    {"code": "076", "label": "Bank Bumi Arta"},
    {"code": "087", "label": "Bank HSBC Indonesia"},
    {"code": "095", "label": "Bank JTrust Indonesia"},
    {"code": "097", "label": "Bank Mayapada International"},
    {"code": "110", "label": "BJB"},
    {"code": "111", "label": "Bank DKI"},
    {"code": "112", "label": "Bank DIY"},
    {"code": "112S", "label": "Bank DIY Syariah"},
    {"code": "113", "label": "Bank Jateng"},
    {"code": "114", "label": "Bank Jatim"},
    {"code": "114S", "label": "Bank Jatim Syariah"},
    {"code": "115", "label": "Bank Jambi"},
    {"code": "115S", "label": "Bank Jambi Syariah"},
    {"code": "116", "label": "Bank Aceh"},
    {"code": "117", "label": "Bank Sumut"},
    {"code": "117S", "label": "Bank Sumut Syariah"},
    {"code": "118", "label": "Bank Nagari"},
    {"code": "118S", "label": "Bank Nagari Syariah"},
    {"code": "119", "label": "Bank Riau"},
    {"code": "120", "label": "Bank Sumsel Babel"},
    {"code": "120S", "label": "Bank Sumsel Babel Syariah"},
    {"code": "121", "label": "Bank Lampung"},
    {"code": "122", "label": "Bank Kalsel"},
    {"code": "122S", "label": "Bank Kalsel Syariah"},
    {"code": "123", "label": "Bank Kalbar"},
    {"code": "123S", "label": "Bank Kalbar Syariah"},
    {"code": "124", "label": "Bank Kaltim"},
    {"code": "124S", "label": "Bank Kaltim Syariah"},
    {"code": "125", "label": "Bank Kalteng"},
    {"code": "126", "label": "Bank Sulselbar"},
    {"code": "126S", "label": "Bank Sulselbar Syariah"},
    {"code": "127", "label": "Bank Sulut"},
    {"code": "128", "label": "Bank NTB"},
    {"code": "129", "label": "Bank Bali"},
    {"code": "130", "label": "Bank NTT"},
    {"code": "131", "label": "Bank Maluku"},
    {"code": "132", "label": "Bank Papua"},
    {"code": "133", "label": "Bank Bengkulu"},
    {"code": "134", "label": "Bank Sulteng"},
    {"code": "135", "label": "Bank Sultra"},
    {"code": "137", "label": "Bank Banten"},
    {"code": "146", "label": "Bank of India Indonesia"},
    {"code": "151", "label": "Bank Mestika"},
    {"code": "152", "label": "Bank Shinhan"},
    {"code": "153", "label": "Bank Sinarmas"},
    {"code": "157", "label": "Bank Maspion Indonesia"},
    {"code": "161", "label": "Bank Ganesha"},
    {"code": "164", "label": "Bank ICBC Indonesia"},
    {"code": "167", "label": "Bank QNB Indonesia"},
    {"code": "200S", "label": "BTN Syariah"},
    {"code": "212", "label": "Bank Woori Saudara"},
    {"code": "213", "label": "Bank SMBC Indonesia"},
    {"code": "253", "label": "Bank Nano Syariah"},
    {"code": "405", "label": "Bank Victoria Syariah"},
    {"code": "425", "label": "BJB Syariah"},
    {"code": "426", "label": "Bank Mega"},
    {"code": "441", "label": "Bank Bukopin"},
    {"code": "472", "label": "Bank Jasa Jakarta"},
    {"code": "484", "label": "Bank KEB Hana"},
    {"code": "485", "label": "Bank MNC"},
    {"code": "498", "label": "Bank SBI Indonesia"},
    {"code": "503", "label": "Bank National Nobu"},
    {"code": "506", "label": "Bank Mega Syariah"},
    {"code": "513", "label": "Bank INA"},
    {"code": "517", "label": "Bank Panin Syariah"},
    {"code": "521", "label": "Bank Syariah Bukopin"},
    {"code": "523", "label": "Bank Sahabat Sampoerna"},
    {"code": "526", "label": "Bank Oke Indonesia"},
    {"code": "531", "label": "Bank Amar Indonesia"},
    {"code": "536", "label": "Bank BCA Syariah"},
    {"code": "542S", "label": "Bank Jago Syariah"},
    {"code": "547", "label": "Bank BTPN Syariah"},
    {"code": "548", "label": "Bank Multiarta Sentosa"},
    {"code": "553", "label": "Bank Hibank Indonesia"},
    {"code": "555", "label": "Bank Index"},
    {"code": "564", "label": "Bank Mandiri Taspen"},
    {"code": "724", "label": "Bank DKI Syariah"},
    {"code": "725", "label": "Bank Jateng Syariah"},
    {"code": "734", "label": "Bank Sinarmas UUS"},
    {"code": "945", "label": "Bank IBK Indonesia"},
    {"code": "949", "label": "Bank CTBC Indonesia"},
]


def supported_providers(bank_code: str) -> list[str]:
    """Which payment gateways accept ``bank_code`` for disbursement."""
    bank_code = (bank_code or "").strip()
    supported: list[str] = []
    if bank_code in XENDIT_BY_CODE:
        supported.append("xendit")
    if bank_code in SENTO_CODES:
        supported.append("sento")
    if any(b["code"] == bank_code for b in BANKS):
        supported.append("dipay")
    return supported


def resolve_for_provider(bank_code: str, provider: str) -> Optional[str]:
    """Translate the canonical bank code into ``provider``'s format.

    Returns None when the provider has no code for this bank — callers
    turn that into DisbursementSkipped / a 422.
    """
    bank_code = (bank_code or "").strip()
    if provider == "dipay":
        return bank_code if any(b["code"] == bank_code for b in BANKS) else None
    if provider == "sento":
        return bank_code if bank_code in SENTO_CODES else None
    if provider == "xendit":
        return XENDIT_BY_CODE.get(bank_code)
    return None
