"""
План/факт текширувчи — "План факат" варағи.

Ишлаш тартиби:
  - Ҳар ой аъзо 2 нарса топширади: ўтган ой ФАКТ + жорий ой ПЛАН
    (масалан: "Сентябрь 2026 — факт" + "Октябрь 2026 — план").
  - Скрипт шу жуфтликни топади, "Топширди" бўлмаган аъзоларни
    ажратади, уларга Telegram'дан ёзади.
  - Аъзони ТЕЛЕФОН РАҚАМИ бўйича топади (ном бўйича эмас).
  - Охирида "Сақланган хабарлар"га куратор бўйича гуруҳланган ҳисобот.

Муҳит ўзгарувчилари:
  TG_API_ID, TG_API_HASH, TG_SESSION - Telegram
  GOOGLE_SA_JSON                     - Google service account JSON (жадвални ёпиқ ўқиш учун)
  SHEET_ID                           - жадвал ID си
  DRY_RUN=1                          - синов: ҳеч кимга ёзмайди, фақат кўрсатади
  PLAN_OY=2026-10                    - (ихтиёрий) қайси ой плани текширилсин; бўлмаса жорий ой
  IMPORT_CONTACTS=1                  - чатда топилмаса, рақамни контактга қўшиб топиш
  MAX_SEND=70                        - бир ишга тушишда энг кўпи нечта хабар
"""
import asyncio
import datetime as dt
import json
import os
import random
import re

import gspread
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.types import InputPhoneContact, User

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION = os.environ["TG_SESSION"]
SA_JSON = json.loads(os.environ["GOOGLE_SA_JSON"])
SHEET_ID = os.environ["SHEET_ID"]
WORKSHEET = os.environ.get("WORKSHEET", "План факат")
DRY_RUN = os.environ.get("DRY_RUN", "1").strip() != "0"   # фақат аниқ "0" бўлса юборади
IMPORT_CONTACTS = os.environ.get("IMPORT_CONTACTS", "1").strip() == "1"
MAX_SEND = int(os.environ.get("MAX_SEND", "70") or "70")

# Ҳар куни бир хил матн бормаслиги учун 3 хил вариантдан бири танланади
XABARLAR = [
    "Ассалому алайкум, {ism}!\n"
    "{fakt_oy} факт ва {plan_oy} план ҳали топширилмаган экан. "
    "Нима бўлди, нега топширилмади?",
    "Ассалому алайкум, {ism}!\n"
    "{fakt_oy} факт ва {plan_oy} планни ҳали кутяпмиз. "
    "Нима сабабдан топширилмаяпти?",
    "{ism}, ассалому алайкум!\n"
    "Жадвалда {fakt_oy} факт ва {plan_oy} план ҳали йўқ. "
    "Нима бўлди, қачон топширасиз?",
]

OYLAR = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
         "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
TASHKENT = dt.timezone(dt.timedelta(hours=5))


def norm(s):
    s = (s or "").replace("\xa0", " ").lower().strip()
    return re.sub(r"\s+", " ", s)


def oy_col(h):
    """'Сентябрь 2026 — факт' -> (2026, 9, 'факт')"""
    m = re.match(r"^([а-яё]+)\s+(\d{4})\s*[—–-]+\s*(план|факт)$", norm(h))
    if m and m.group(1) in OYLAR:
        return int(m.group(2)), OYLAR.index(m.group(1)) + 1, m.group(3)
    return None


def phone_norm(s):
    # Катакда бир нечта рақам бўлиши мумкин ("90 123 45 67, 91 ...") — биринчисини оламиз
    first = re.split(r"[,;/\n]", s or "")[0]
    d = re.sub(r"\D", "", first)
    if len(d) == 9:                          # 974446533 -> 998974446533 (Ўзбекистон, кодсиз)
        d = "998" + d
    elif len(d) == 11 and d.startswith("8"):  # 8 701 ... -> 7 701 ... (Россия/Қозоғистон)
        d = "7" + d[1:]
    return d if len(d) >= 10 else ""


def oy_nomi(y, m):
    return f"{OYLAR[m - 1].capitalize()} {y}"


def find_col(idx, *starts):
    """Сарлавҳани бошланиши бўйича топади ('тел' -> 'тел. рақами')."""
    for h, i in idx.items():
        if any(h.startswith(s) for s in starts):
            return i
    return None


def read_sheet():
    gc = gspread.service_account_from_dict(SA_JSON)
    sh = gc.open_by_key(SHEET_ID)
    ws = next((w for w in sh.worksheets() if norm(w.title) == norm(WORKSHEET)), None)
    if not ws:
        raise SystemExit(f"Варақ топилмади: {WORKSHEET}. Бор варақлар: {[w.title for w in sh.worksheets()]}")
    values = ws.get_all_values()

    hdr_i = next((i for i, r in enumerate(values) if any(norm(c) == "и.ф.о." for c in r)), None)
    if hdr_i is None:
        raise SystemExit("Сарлавҳа қатори топилмади ('И.Ф.О.' устуни йўқ)")
    hdr = values[hdr_i]
    idx = {}
    for i, h in enumerate(hdr):
        idx.setdefault(norm(h), i)
    oy_cols = {oy_col(h): i for i, h in enumerate(hdr) if oy_col(h)}
    return values[hdr_i + 1:], idx, oy_cols, len(hdr)


async def main():
    # Қайси жуфтлик: ўтган ой факт + жорий ой план
    if os.environ.get("PLAN_OY", "").strip():
        py, pm = map(int, os.environ["PLAN_OY"].strip().split("-"))
    else:
        t = dt.datetime.now(TASHKENT)
        py, pm = t.year, t.month
    fy, fm = (py, pm - 1) if pm > 1 else (py - 1, 12)

    rows, idx, oy_cols, width = read_sheet()
    c_fakt = oy_cols.get((fy, fm, "факт"))
    c_plan = oy_cols.get((py, pm, "план"))
    if c_fakt is None or c_plan is None:
        raise SystemExit(f"Устун топилмади: {oy_nomi(fy, fm)} — факт / {oy_nomi(py, pm)} — план")

    c_name = idx["и.ф.о."]
    c_tel = find_col(idx, "тел")
    if c_tel is None:
        raise SystemExit("'Тел. рақами' устуни топилмади")
    c_kurator, c_klub = find_col(idx, "куратор"), find_col(idx, "клуб")

    yozmaganlar, raqamsiz = [], []
    for r in rows:
        r = r + [""] * (width - len(r))
        ism = (r[c_name] or "").strip()
        if not ism or not re.search(r"[A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ]", ism):
            continue  # бўш ёки "Жами" қаторлари
        if norm(r[c_fakt]) == "топширди" and norm(r[c_plan]) == "топширди":
            continue
        a = {
            "ism": ism, "tel": phone_norm(r[c_tel]),
            "kurator": (r[c_kurator] if c_kurator is not None else "").strip() or "Кураторсиз",
        }
        (yozmaganlar if a["tel"] else raqamsiz).append(a)

    print(f"{oy_nomi(fy, fm)} факт / {oy_nomi(py, pm)} план — топширмаганлар: "
          f"{len(yozmaganlar) + len(raqamsiz)} (рақамсиз: {len(raqamsiz)})")

    async with TelegramClient(StringSession(SESSION), API_ID, API_HASH) as client:
        me = await client.get_me()
        by_phone = {}
        async for d in client.iter_dialogs():
            e = d.entity
            if isinstance(e, User) and e.phone and not e.bot:
                by_phone[e.phone] = e

        yozildi, topilmadi, qoldi = [], [], []
        toxtat = None  # Telegram чеклов қўйса, шу ерда сабаб
        for a in yozmaganlar:
            if toxtat or len(yozildi) >= MAX_SEND:
                qoldi.append(a)
                continue
            if me.phone and a["tel"] == me.phone:
                continue  # аккаунт эгасининг ўзи
            target = by_phone.get(a["tel"])
            if not target and IMPORT_CONTACTS and not DRY_RUN:
                try:
                    res = await client(ImportContactsRequest([InputPhoneContact(
                        client_id=random.randrange(1, 2**62), phone="+" + a["tel"],
                        first_name=a["ism"], last_name="")]))
                    target = res.users[0] if res.users else None
                except errors.FloodWaitError as ex:
                    toxtat = f"контакт қўшишда чеклов ({ex.seconds} сония)"
                    qoldi.append(a)
                    continue
                except Exception as ex:
                    print(f"  ! {a['ism']}: {ex}")
            if not target and not DRY_RUN:
                topilmadi.append(a)
                print(f"  ? {a['ism']}: Telegram'да топилмади")
                continue

            matn = random.choice(XABARLAR).format(ism=a["ism"], fakt_oy=oy_nomi(fy, fm), plan_oy=oy_nomi(py, pm))
            if DRY_RUN:
                belgi = "чатда бор" if target else "чатда йўқ → контакт орқали қидирилади"
                print(f"  [СИНОВ] {a['ism']} ({belgi})")
                yozildi.append(a)
                continue
            try:
                await client.send_message(target, matn)
            except errors.FloodWaitError as ex:
                toxtat = f"Telegram {ex.seconds} сония кутишни сўради"
                qoldi.append(a)
                continue
            except errors.PeerFloodError:
                toxtat = "Telegram спам чекловини қўйди (PeerFlood)"
                qoldi.append(a)
                continue
            except Exception as ex:
                print(f"  ! {a['ism']}: {ex}")
                topilmadi.append(a)
                continue
            print(f"  ✓ {a['ism']}")
            yozildi.append(a)
            await asyncio.sleep(random.uniform(15, 25))  # спам бўлмаслиги учун

        # Ҳисобот — куратор бўйича
        def guruh(lst):
            out = {}
            for a in lst:
                out.setdefault(a["kurator"], []).append(a["ism"])
            return "\n".join(f"\n👤 {k} ({len(v)}):\n" + "\n".join(f"  • {x}" for x in v)
                             for k, v in sorted(out.items()))

        jami = len(yozmaganlar) + len(raqamsiz)
        hisobot = (("🧪 СИНОВ — ҳеч кимга ёзилмади\n\n" if DRY_RUN else "")
                   + f"📋 {oy_nomi(fy, fm)} факт / {oy_nomi(py, pm)} план\n"
                   + f"Топширмаганлар: {jami}\n"
                   + (f"\n✉️ {'Ёзилади' if DRY_RUN else 'Ёзилди'} ({len(yozildi)}):" + guruh(yozildi) if yozildi else "")
                   + (f"\n\n❓ Telegram'да топилмади ({len(topilmadi)}):" + guruh(topilmadi) if topilmadi else "")
                   + (f"\n\n📵 Жадвалда рақам йўқ ({len(raqamsiz)}):" + guruh(raqamsiz) if raqamsiz else "")
                   + (f"\n\n⏸ Эртага ёзилади ({len(qoldi)}):" + guruh(qoldi) if qoldi else "")
                   + (f"\n\n⚠️ Тўхтатилди: {toxtat}" if toxtat else "")
                   + ("\nҲамма топширган ✅" if jami == 0 else ""))
        for i in range(0, len(hisobot), 4000):
            await client.send_message("me", hisobot[i:i + 4000])


if __name__ == "__main__":
    asyncio.run(main())
