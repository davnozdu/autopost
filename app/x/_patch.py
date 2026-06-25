"""Рантайм-патч twikit для генерации x-client-transaction-id.

С ~18.03.2026 X изменил структуру ondemand.s.js, и twikit 2.3.3 падает с
«Couldn't get KEY_BYTE indices» (twikit/issues/408). Фикс известен (upstream
XClientTransaction), но в релиз на PyPI его не выпустили. Накладываем его в
рантайме: новый поиск имени ondemand-файла в два шага (индекс → хэш) + обновлённый
INDICES_REGEX. Применяется только к затронутым версиям (<= 2.3.3); если выйдет
исправленный релиз и пользователь обновит twikit — патч не трогаем.
"""

import re

_FIXED = False


def _version_affected() -> bool:
    try:
        from app.x.updater import installed_version

        v = installed_version()
        if not v:
            return False
        parts = tuple(int(x) for x in v.split(".")[:3])
        return parts <= (2, 3, 3)
    except Exception:
        return True  # не смогли определить — лучше пропатчить


def apply_patch() -> bool:
    """Наложить фикс get_indices/regex на ClientTransaction twikit (идемпотентно)."""
    global _FIXED
    if _FIXED:
        return True
    if not _version_affected():
        return False
    try:
        from twikit.x_client_transaction import transaction as tx
    except Exception:
        return False
    if getattr(tx, "_autopost_patched", False):
        _FIXED = True
        return True

    tx.ON_DEMAND_FILE_REGEX = re.compile(
        r""",(\d+):["']ondemand\.s["']""", flags=(re.VERBOSE | re.MULTILINE)
    )
    tx.ON_DEMAND_HASH_PATTERN = r',{}:\"([0-9a-f]+)\"'
    tx.INDICES_REGEX = re.compile(
        r"""(\(\w{1,2}\[(\d{1,2})\],\s*16\))+""", flags=(re.VERBOSE | re.MULTILINE)
    )

    async def get_indices(self, home_page_response, session, headers):
        key_byte_indices = []
        response = self.validate_response(home_page_response) or self.home_page_response
        response_str = str(response)
        on_demand_file = tx.ON_DEMAND_FILE_REGEX.search(response_str)
        if on_demand_file:
            idx = on_demand_file.group(1)
            hash_match = re.compile(tx.ON_DEMAND_HASH_PATTERN.format(idx)).search(response_str)
            if hash_match:
                filename = hash_match.group(1)
                url = (
                    "https://abs.twimg.com/responsive-web/client-web/"
                    f"ondemand.s.{filename}a.js"
                )
                resp = await session.request(method="GET", url=url, headers=headers)
                for item in tx.INDICES_REGEX.finditer(str(resp.text)):
                    key_byte_indices.append(item.group(2))
        if not key_byte_indices:
            raise Exception("Couldn't get KEY_BYTE indices")
        key_byte_indices = list(map(int, key_byte_indices))
        return key_byte_indices[0], key_byte_indices[1:]

    tx.ClientTransaction.get_indices = get_indices
    tx._autopost_patched = True
    _FIXED = True
    return True
