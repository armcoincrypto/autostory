"""
Stable, human-varied first-message openers for AI Agent (OTC desk).

Selection is deterministic per task + goal + turn so auto-loop ticks do not rewrite the opener.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

_OPENER_PROFILES = frozenset({"friendly", "neutral", "assertive", "aggressive_trader"})


def _eff_field_known(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        s = v.strip().lower()
        if not s or s in ("unknown", "null", "n/a", "—"):
            return False
    return True


def map_task_tone_to_opener_profile(tone: Optional[str]) -> str:
    t = (tone or "professional").strip().lower()
    if t in ("friendly",):
        return "friendly"
    if t in ("strict", "assertive"):
        return "assertive"
    if t in ("aggressive", "aggressive_trader", "trader"):
        return "aggressive_trader"
    if t in ("neutral", "professional"):
        return "neutral"
    return "neutral"


def resolve_opener_language(task_language: Optional[str], goal: str) -> str:
    raw = (task_language or "auto").strip().lower()
    if raw in ("hy", "am", "armenian"):
        return "hy"
    if raw in ("ru", "rus", "russian"):
        return "ru"
    if raw in ("en", "english"):
        return "en"
    if raw != "auto":
        return "en"
    g = goal or ""
    if re.search(r"[\u0530-\u058F]", g):
        return "hy"
    if re.search(r"[\u0400-\u04FF]", g):
        return "ru"
    return "en"


def stable_opener_variant_index(
    *,
    task_id: int,
    target: str,
    goal: str,
    turn_number: int,
    lang: str,
    profile: str,
    pool_len: int,
) -> int:
    if pool_len <= 0:
        return 0
    payload = f"{int(task_id)}:{target or ''}:{goal or ''}:{int(turn_number)}:{lang}:{profile}"
    h = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return int(h[:12], 16) % pool_len


def _pick_pool(
    lang: str,
    profile: str,
    slot: str,
    pools: dict[tuple[str, str, str], tuple[str, ...]],
) -> tuple[str, ...]:
    key = (lang, profile, slot)
    if key in pools:
        return pools[key]
    if (lang, "neutral", slot) in pools:
        return pools[(lang, "neutral", slot)]
    if ("en", profile, slot) in pools:
        return pools[("en", profile, slot)]
    return pools[("en", "neutral", slot)]


def _format_opener(template: str, *, asset: str = "") -> str:
    return template.format(asset=asset or "USDT")


# --- Pools: 6 variants each (lang, profile, slot). Max 2 "?" per template (Telegram opener cap). ---

def _pools() -> dict[tuple[str, str, str], tuple[str, ...]]:
    p: dict[tuple[str, str, str], tuple[str, ...]] = {}

    # English buy + asset
    p[("en", "friendly", "buy_asset")] = (
        "Hi, how are you?\n\nI'm looking to buy {asset}.\nWhat volume can you provide and what rate are you offering?",
        "Hello, hope you're well.\n\nI'm interested in buying {asset}.\nWhat amount can you show and at what rate?",
        "Hey — good to connect.\n\nI want to pick up some {asset}.\nWhat's your clip and how are you quoting?",
        "Hi there.\n\nI'm in the market for {asset} on the buy side.\nHow much can you work and where are you priced?",
        "Morning.\n\nLooking to buy {asset} if the level makes sense.\nWhat size is available and what's your number?",
        "Hi.\n\nQuick one — I'm buying {asset}.\nWhat volume can you do and at what rate?",
    )
    p[("en", "neutral", "buy_asset")] = (
        "Hello.\n\nI'm looking to buy {asset}.\nWhat volume can you offer and what rate?",
        "Hi.\n\nWe want to buy {asset}.\nPlease share available size and your rate.",
        "Good day.\n\nInterested in buying {asset}.\nWhat's your workable volume and price?",
        "Hi.\n\nLooking for a {asset} offer on the buy side.\nHow much can you show and at what level?",
        "Hello.\n\nNeed a quote to buy {asset}.\nAvailable amount and rate?",
        "Hi.\n\n{asset} purchase inquiry.\nWhat size can you deliver and at what rate?",
    )
    p[("en", "assertive", "buy_asset")] = (
        "Hi.\n\nI need to buy {asset} — what's your size and rate?",
        "Hello.\n\nSend me your {asset} bid.\nVolume and level?",
        "Hi.\n\nLooking to buy {asset} today.\nWhat can you show and where are you priced?",
        "Quick note.\n\n{asset} buyer here.\nClip and rate?",
        "Hi.\n\nI'm taking {asset} on the bid.\nHow much and at what price?",
        "Hello.\n\nNeed {asset} liquidity.\nSize and quote?",
    )
    p[("en", "aggressive_trader", "buy_asset")] = (
        "{asset} bid live — size and level?\n\nPing me with what you can show.",
        "Buying {asset}.\n\nWhat's your clip and where are you marking it?",
        "Need an offer on {asset}.\n\nVolume + rate — go.",
        "{asset} — I'm on the buy.\n\nHit me with size and price.",
        "Taking {asset}.\n\nHow much and at what rate?",
        "Bid for {asset}.\n\nShow depth and your number.",
    )

    # English sell + asset
    p[("en", "friendly", "sell_asset")] = (
        "Hi, how are you?\n\nI'm looking to sell {asset}.\nWhat volume are you after and what rate can you offer?",
        "Hello.\n\nI'd like to sell some {asset}.\nWhat size do you need and what level works for you?",
        "Hey.\n\nOffering {asset} on my side.\nWhat amount are you looking for and at what rate?",
        "Hi there.\n\nI have {asset} for sale.\nWhat's your demand and price?",
        "Good day.\n\nSelling {asset} if terms line up.\nWhat volume and rate are you thinking?",
        "Hi.\n\nQuick one — selling {asset}.\nWhat size do you want and what's your bid?",
    )
    p[("en", "neutral", "sell_asset")] = (
        "Hello.\n\nI'm looking to sell {asset}.\nWhat volume do you need and at what rate?",
        "Hi.\n\nOffering {asset}.\nRequested size and your price?",
        "Good day.\n\n{asset} available to sell.\nHow much and at what level?",
        "Hi.\n\nSell-side on {asset}.\nVolume and rate?",
        "Hello.\n\n{asset} disposal.\nWhat quantity and quote?",
        "Hi.\n\nI can sell {asset}.\nWhat's your clip and price?",
    )
    p[("en", "assertive", "sell_asset")] = (
        "Hi.\n\nSelling {asset} — what's your size and bid?",
        "Hello.\n\n{asset} offered.\nHow much do you want and at what rate?",
        "Quick one.\n\nOffloading {asset}.\nVolume and level?",
        "Hi.\n\n{asset} sell.\nClip and price?",
        "Hello.\n\nI've got {asset} to move.\nSize and rate?",
        "Hi.\n\n{asset} on the offer.\nWhat are you paying?",
    )
    p[("en", "aggressive_trader", "sell_asset")] = (
        "{asset} offered — size and bid?\n\nLet me know what you're paying.",
        "Selling {asset}.\n\nHow much and at what level?",
        "Offloading {asset}.\n\nVolume + bid — go.",
        "{asset} sell — what's your clip and price?",
        "Got {asset}.\n\nHit me with size and rate.",
        "Offer on {asset}.\n\nDemand and number?",
    )

    # Russian buy + asset
    p[("ru", "friendly", "buy_asset")] = (
        "Привет, как дела?\n\nХочу купить {asset}.\nКакой объем можете дать и по какому курсу?",
        "Здравствуйте, надеюсь, у вас все хорошо.\n\nИнтересует покупка {asset}.\nКакой объем доступен и какой курс?",
        "Привет.\n\nИщу {asset} на покупку.\nКакой размер можете предложить и по какой цене?",
        "Добрый день.\n\nХотел бы купить {asset}.\nКакой объем и курс?",
        "Приветствую.\n\nНужен {asset}, покупка.\nСколько можете отдать и по какому курсу?",
        "Привет.\n\nБеру {asset}.\nКакой объем и какая котировка?",
    )
    p[("ru", "neutral", "buy_asset")] = (
        "Здравствуйте.\n\nХочу купить {asset}.\nОбъем и курс, пожалуйста?",
        "Добрый день.\n\nПокупка {asset}.\nКакой объем и по какой цене?",
        "Привет.\n\nИнтересует {asset} на покупку.\nДоступный объем и курс?",
        "Здравствуйте.\n\nЗаявка на покупку {asset}.\nРазмер и курс?",
        "Добрый день.\n\nНужен офер на {asset}.\nОбъем и уровень цены?",
        "Привет.\n\nПокупаю {asset}.\nСколько можете и по какому курсу?",
    )
    p[("ru", "assertive", "buy_asset")] = (
        "Привет.\n\nПокупаю {asset} — объем и курс?",
        "Здравствуйте.\n\nНужен {asset}. Какой объем и цена?",
        "Добрый день.\n\nБеру {asset}.\nРазмер и курс?",
        "Привет.\n\n{asset} на покупку — что можете дать и по какой цене?",
        "Здравствуйте.\n\nЗапрос на {asset}.\nОбъем и котировка?",
        "Привет.\n\n{asset} bid — объем и уровень?",
    )
    p[("ru", "aggressive_trader", "buy_asset")] = (
        "Беру {asset}.\n\nОбъем и уровень — в студию.",
        "Покупка {asset}.\n\nСколько и по какой цене?",
        "Нужен {asset}.\n\nКлип и курс?",
        "{asset} — на покупку.\n\nРазмер и цена?",
        "Bid по {asset}.\n\nЧто можете показать и по какому курсу?",
        "Ищу {asset}.\n\nОбъем + курс.",
    )

    # Russian sell + asset
    p[("ru", "friendly", "sell_asset")] = (
        "Привет, как дела?\n\nХочу продать {asset}.\nКакой объем вам нужен и по какому курсу готовы?",
        "Здравствуйте.\n\nПродаю {asset}.\nКакой размер интересует и какая цена?",
        "Привет.\n\nЕсть {asset} к продаже.\nСколько вам нужно и по какому курсу?",
        "Добрый день.\n\nГотов продать {asset}.\nОбъем и курс?",
        "Приветствую.\n\nПо {asset} на продажу.\nКакой объем и ставка?",
        "Привет.\n\nОтдаю {asset}.\nСколько берете и по какой цене?",
    )
    p[("ru", "neutral", "sell_asset")] = p[("ru", "friendly", "sell_asset")]  # reuse; hash still varies by profile via index
    p[("ru", "assertive", "sell_asset")] = (
        "Продаю {asset}.\n\nОбъем и ваш курс?",
        "Есть {asset}.\n\nСколько нужно и по какой цене?",
        "{asset} на продажу.\n\nРазмер и ставка?",
        "Продажа {asset}.\n\nИнтересующий объем и цена?",
        "Отдаю {asset}.\n\nКлип и курс?",
        "{asset} sell.\n\nОбъем и бид?",
    )
    p[("ru", "aggressive_trader", "sell_asset")] = (
        "Продаю {asset}.\n\nСколько и по какой цене?",
        "{asset} в наличии.\n\nОбъем и ваш уровень?",
        "Sell {asset}.\n\nРазмер и бид?",
        "Отдаю {asset}.\n\nКлип и цена?",
        "{asset} offer.\n\nСпрос и ставка?",
        "Гоню {asset}.\n\nСколько берете и по чем?",
    )

    # Armenian buy + asset (user sample + variants)
    p[("hy", "friendly", "buy_asset")] = (
        "Բարև, ո՞նց եք։\n\nՈւզում եմ {asset} գնել։\nԻ՞նչ ծավալ կարող եք տրամադրել և ի՞նչ կուրսով։",
        "Բարև ձեզ։\n\nՀետաքրքիր է {asset} գնելը։\nԻնչ ծավալ ունեք և ինչ գնով։",
        "Բարև։\n\nՊետք է {asset} գնեմ։\nԻնչ քանակ կարող եք տալ և ինչ փոխարժեքով։",
        "Բարև, լավ եք։\n\nՈւզում եմ {asset} վերցնել։\nԾավալը և կուրսը կասե՞ք։",
        "Բարև ձեզ, հուսով եմ՝ լավ եք։\n\nԳնման {asset}։\nԻնչ ծավալ կա և ինչ rate։",
        "Բարև։\n\n{asset} գնելու կողմից եմ։\nՔանի՞սը կարող եք անել և ինչ գնով։",
    )
    p[("hy", "neutral", "buy_asset")] = (
        "Բարև։\n\nՈւզում եմ գնել {asset}։\nԽնդրում եմ նշել ծավալը և կուրսը։",
        "Բարև ձեզ։\n\n{asset} գնման հարցով։\nՀասանելի ծավալ և փոխարժեք։",
        "Բարև։\n\nԳնում եմ {asset}։\nԻնչ ծավալ ու ինչ գին։",
        "Բարև ձեզ։\n\nՀայտ {asset} գնելու համար։\nԾավալը և գինը։",
        "Բարև։\n\n{asset} — գնող եմ։\nՔանակ և կուրս։",
        "Բարև ձեզ։\n\nՊետք է {asset}։\nՏվյալներ ծավալի և կուրսի մասին։",
    )
    p[("hy", "assertive", "buy_asset")] = (
        "Բարև։\n\nԳնում եմ {asset} — ծավալը և կուրսը։",
        "Բարև ձեզ։\n\nՊետք է {asset}։ Քանի՞սը և ինչ գնով։",
        "Բարև։\n\n{asset} bid — ծավալ և level։",
        "Բարև ձեզ։\n\nՈւզում եմ {asset}։ Ինչ ունեք և ինչ գնով։",
        "Բարև։\n\n{asset} գնել — ասեք ծավալը և կուրսը։",
        "Բարև ձեզ։\n\n{asset} — գնող։ Քանակ և գին։",
    )
    p[("hy", "aggressive_trader", "buy_asset")] = (
        "Գնում եմ {asset}։\n\nՔանակ + կուրս — գրեք։",
        "{asset} — bid։\n\nԻնչ կարող եք տալ և ինչ գնով։",
        "Պետք է {asset}։\n\nՔանի՞սը և rate։",
        "Ուզում եմ {asset}։\n\nՔանակ և գին — կարճ։",
        "{asset} գնել։\n\nՔանակ և կուրս — կարճ։",
        "Բերեք {asset} առաջարկը։\n\nծավալ/կուրս։",
    )

    # Armenian sell + asset
    p[("hy", "friendly", "sell_asset")] = (
        "Բարև, ո՞նց եք։\n\nՈւզում եմ վաճառել {asset}։\nԻնչ ծավալ է պետք և ինչ գնով եք առաջարկում։",
        "Բարև ձեզ։\n\nՈւնեմ {asset} վաճառքի։\nՔանի՞սը պետք է և ինչ գնով։",
        "Բարև։\n\nՎաճառում եմ {asset}։\nԻնչ ծավալ ու ինչ գին։",
        "Բարև ձեզ։\n\n{asset} հասանելի է։\nՊահանջվող ծավալը և գինը։",
        "Բարև։\n\nՎաճառք {asset}։\nՔանակ և առաջարկվող կուրս։",
        "Բարև ձեզ։\n\nԿարող եմ տալ {asset}։\nՈրքա՞ն է պետք և ինչ գնով։",
    )
    p[("hy", "neutral", "sell_asset")] = (
        "Բարև։\n\nՎաճառում եմ {asset}։\nՔանակ և գին։",
        "Բարև ձեզ։\n\n{asset} վաճառք։\nՊահանջ և կուրս։",
        "Բարև։\n\nՈւնեմ {asset}։\nԻնչ ծավալ և ինչ գնով։",
        "Բարև ձեզ։\n\nSell {asset}։\nՔանակ և bid։",
        "Բարև։\n\n{asset} — վաճառք։\nՔանակ և գին։",
        "Բարև ձեզ։\n\nՎաճառքի {asset}։\nՊահանջվող ծավալ/գին։",
    )
    p[("hy", "assertive", "sell_asset")] = p[("hy", "neutral", "sell_asset")]
    p[("hy", "aggressive_trader", "sell_asset")] = (
        "Վաճառում եմ {asset}։\n\nՔանակ և bid։",
        "{asset} sell։\n\nՈրքա՞ն և ինչ գնով։",
        "Ունեմ {asset}։\n\nՊահանջ և գին — արագ։",
        "Վաճառք {asset}։\n\nՔանակ + level։",
        "{asset} offer։\n\nՔանի՞սը և գինը։",
        "Տամ {asset}։\n\nՈրքան ու ինչ գնով։",
    )

    # buy known, asset unknown — never ask buy/sell
    p[("en", "friendly", "buy_no_asset")] = (
        "Hi, how are you?\n\nI'm looking to buy.\nWhich coin are you working with, and what volume + rate?",
        "Hello.\n\nI want to buy — still picking the leg.\nWhat asset do you quote, and size + level?",
        "Hey.\n\nOn the buy side today.\nWhich pair and what volume + rate?",
        "Hi.\n\nBuying flow — need a quote.\nAsset, size, and rate?",
        "Good day.\n\nLooking to buy something spot.\nWhat are you showing and at what price?",
        "Hi there.\n\nBuy inquiry.\nWhich coin, volume, and rate?",
    )
    p[("en", "neutral", "buy_no_asset")] = (
        "Hello.\n\nI'm looking to buy.\nPlease specify asset, available volume, and rate.",
        "Hi.\n\nBuy-side request.\nAsset, size, and price?",
        "Good day.\n\nPurchase inquiry.\nWhich instrument, volume, and rate?",
        "Hi.\n\nWe need a buy quote.\nAsset + size + level?",
        "Hello.\n\nBuying.\nCoin, clip, and rate?",
        "Hi.\n\nBid request.\nWhat are you quoting — size and price?",
    )
    p[("en", "assertive", "buy_no_asset")] = (
        "Hi.\n\nBuying — what are you quoting, size and rate?",
        "Hello.\n\nNeed a buy offer.\nAsset, volume, price?",
        "Quick.\n\nOn the bid.\nPair + clip + level?",
        "Hi.\n\nBuy flow.\nInstrument and numbers?",
        "Hello.\n\nTaking bids.\nWhat leg, how much, at what price?",
        "Hi.\n\nPurchase.\nAsset + size + rate?",
    )
    p[("en", "aggressive_trader", "buy_no_asset")] = (
        "Buying.\n\nWhat leg — size and level?",
        "Bid.\n\nAsset + clip + rate — go.",
        "On the buy.\n\nWhat are you showing?",
        "Need size.\n\nPair and price?",
        "Buy inquiry.\n\nNumbers?",
        "Taking offers.\n\nAsset, volume, rate?",
    )

    p[("ru", "friendly", "buy_no_asset")] = (
        "Привет, как дела?\n\nХочу купить.\nКакую монету котируете, объем и курс?",
        "Здравствуйте.\n\nПокупка.\nКакой актив, размер и цена?",
        "Привет.\n\nНа покупке.\nЧто предлагаете по объему и курсу?",
        "Добрый день.\n\nИнтересует покупка.\nИнструмент, объем, курс?",
        "Привет.\n\nБеру.\nКакая пара, сколько и по какой цене?",
        "Здравствуйте.\n\nНужен bid.\nАктив, объем, уровень?",
    )
    p[("ru", "neutral", "buy_no_asset")] = p[("ru", "friendly", "buy_no_asset")]
    p[("ru", "assertive", "buy_no_asset")] = (
        "Покупка.\n\nАктив, объем, курс?",
        "На bid.\n\nЧто котируете — размер и цена?",
        "Беру.\n\nПара и цифры?",
        "Нужен офер.\n\nИнструмент, объем, уровень?",
        "Buy.\n\nЧто есть по объему и цене?",
        "Заявка на покупку.\n\nАктив + размер + курс?",
    )
    p[("ru", "aggressive_trader", "buy_no_asset")] = (
        "Bid.\n\nАктив, клип, курс.",
        "Покупаю.\n\nЧто показываете?",
        "На покупке.\n\nЦифры?",
        "Нужен офер.\n\nПара и level.",
        "Беру.\n\nОбъем + цена.",
        "Куплю.\n\nЧто есть?",
    )

    p[("hy", "friendly", "buy_no_asset")] = (
        "Բարև, ո՞նց եք։\n\nՈւզում եմ գնել։\nՈ՞ր մետաղադրամն եք կոտրում, ծավալը և կուրսը։",
        "Բարև ձեզ։\n\nԳնման կողմից եմ։\nԻնչ ակտիվ, ծավալ և գին։",
        "Բարև։\n\nԳնում եմ։\nԱկտիվ, քանակ և rate։",
        "Բարև ձեզ։\n\nՊետք է գնել։\nԶույգը, ծավալը, գինը։",
        "Բարև։\n\nBuy — ինչ ունեք ծավալով և գնով։",
        "Բարև ձեզ։\n\nԳնող եմ։\nԱկտիվ + ծավալ + կուրս։",
    )
    p[("hy", "neutral", "buy_no_asset")] = p[("hy", "friendly", "buy_no_asset")]
    p[("hy", "assertive", "buy_no_asset")] = p[("hy", "friendly", "buy_no_asset")]
    p[("hy", "aggressive_trader", "buy_no_asset")] = (
        "Գնում եմ։\n\nԱկտիվ, ծավալ, կուրս։",
        "Bid։\n\nԻնչ ունեք։",
        "Գնող։\n\nԶույգ և цифры։",
        "Պետք է գնել։\n\nՔանակ + գին։",
        "Buy։\n\nԱսեք ակտիվը և level։",
        "Գնման հարց։\n\nՏվյալներ։",
    )

    # sell known, asset unknown
    p[("en", "friendly", "sell_no_asset")] = (
        "Hi, how are you?\n\nI'm looking to sell.\nWhich coin are you after, and what volume + rate?",
        "Hello.\n\nSelling something spot.\nWhat asset do you need, size and price?",
        "Hey.\n\nOn the offer side.\nWhich pair, clip, and level?",
        "Hi.\n\nSell inquiry.\nInstrument, volume, rate?",
        "Good day.\n\nLooking to sell.\nWhat are you bidding for — size and price?",
        "Hi there.\n\nOffloading.\nCoin, amount, and rate?",
    )
    p[("en", "neutral", "sell_no_asset")] = (
        "Hello.\n\nI'm looking to sell.\nAsset, volume, and rate?",
        "Hi.\n\nSell-side.\nWhich instrument, size, price?",
        "Good day.\n\nDisposal inquiry.\nCoin, clip, bid?",
        "Hi.\n\nOffering liquidity.\nWhat leg, volume, rate?",
        "Hello.\n\nSale.\nAsset + size + level?",
        "Hi.\n\nSell quote needed.\nPair and numbers?",
    )
    p[("en", "assertive", "sell_no_asset")] = (
        "Hi.\n\nSelling — what do you need, size and bid?",
        "Hello.\n\nOffer side.\nAsset, volume, price?",
        "Quick.\n\nOffloading.\nPair + clip + bid?",
        "Hi.\n\nSell.\nInstrument and numbers?",
        "Hello.\n\nWhat are you buying — size and rate?",
        "Hi.\n\nSale flow.\nCoin, volume, bid?",
    )
    p[("en", "aggressive_trader", "sell_no_asset")] = (
        "Selling.\n\nWhat leg — size and bid?",
        "Offer.\n\nAsset + clip + price?",
        "Offloading.\n\nPair and numbers?",
        "Sell.\n\nWhat are you paying?",
        "Moving size.\n\nInstrument and bid?",
        "Sale.\n\nDetails?",
    )

    p[("ru", "friendly", "sell_no_asset")] = (
        "Привет, как дела?\n\nХочу продать.\nКакую монету нужно, объем и курс?",
        "Здравствуйте.\n\nПродаю.\nКакой актив, размер и цена?",
        "Привет.\n\nНа продаже.\nПара, объем, ставка?",
        "Добрый день.\n\nПродажа.\nИнструмент, объем, курс?",
        "Привет.\n\nSell.\nЧто берете, сколько и по какой цене?",
        "Здравствуйте.\n\nОффер.\nАктив, клип, бид?",
    )
    p[("ru", "neutral", "sell_no_asset")] = p[("ru", "friendly", "sell_no_asset")]
    p[("ru", "assertive", "sell_no_asset")] = (
        "Продаю.\n\nАктив, объем, бид?",
        "Sell.\n\nЧто нужно — размер и цена?",
        "Оффер.\n\nПара и цифры?",
        "Продажа.\n\nИнструмент, объем, курс?",
        "Отдаю.\n\nСпрос и ставка?",
        "Sell-side.\n\nАктив + клип + бид?",
    )
    p[("ru", "aggressive_trader", "sell_no_asset")] = (
        "Sell.\n\nПара, объем, бид.",
        "Продаю.\n\nЧто берете?",
        "Оффер.\n\nЦифры?",
        "Продажа.\n\nАктив и бид.",
        "Отдаю ликвидность.\n\nРазмер и цена?",
        "Sell.\n\nКлип + level.",
    )

    p[("hy", "friendly", "sell_no_asset")] = (
        "Բարև, ո՞նց եք։\n\nՈւզում եմ վաճառել։\nՈ՞ր ակտիվ է պետք, ծավալը և գինը։",
        "Բարև ձեզ։\n\nՎաճառք։\nԱկտիվ, քանակ, գին։",
        "Բարև։\n\nSell — ինչ զույգ, ծավալ, կուրս։",
        "Բարև ձեզ։\n\nՎաճառում եմ։\nԱկտիվ + ծավալ + bid։",
        "Բարև։\n\nOffer side։\nՄետաղադրամ, ծավալ, գին։",
        "Բարև ձեզ։\n\nՊետք է վաճառել։\nՏվյալներ։",
    )
    p[("hy", "neutral", "sell_no_asset")] = p[("hy", "friendly", "sell_no_asset")]
    p[("hy", "assertive", "sell_no_asset")] = p[("hy", "friendly", "sell_no_asset")]
    p[("hy", "aggressive_trader", "sell_no_asset")] = (
        "Վաճառում եմ։\n\nԱկտիվ, ծավալ, bid։",
        "Sell։\n\nԻնչ եք առաջարկում։",
        "Offer։\n\nՔանակ + գին։",
        "Վաճառք։\n\nԶույգ և цифры։",
        "Ունեմ լիկվիդ։\n\nՊահանջ և գին։",
        "Sell-side։\n\nՏվյալներ։",
    )

    # asset known, side unknown — may ask direction (not "which asset" when asset set)
    p[("en", "friendly", "asset_no_side")] = (
        "Hi, how are you?\n\nQuick one on {asset}.\nBuy or sell on your end — what volume and rate works for you?",
        "Hello.\n\nTouching base on {asset}.\nAre you bidding or offering, and what's your size + level?",
        "Hey.\n\nRe {asset}.\nWhich way are you, and volume + price?",
        "Hi.\n\nAbout {asset}.\nBuy or sell — size and rate?",
        "Good day.\n\n{asset} check-in.\nDirection, clip, and quote?",
        "Hi there.\n\nOn {asset}.\nYour side and numbers?",
    )
    p[("en", "neutral", "asset_no_side")] = (
        "Hello.\n\nRegarding {asset}.\nBuy or sell, and volume + rate?",
        "Hi.\n\n{asset} inquiry.\nDirection, size, price?",
        "Good day.\n\n{asset}.\nSide, volume, and level?",
        "Hi.\n\nQuick on {asset}.\nBid or offer — numbers?",
        "Hello.\n\n{asset} leg.\nWhich way, size, rate?",
        "Hi.\n\n{asset} — your flow?\nVolume and price?",
    )
    p[("en", "assertive", "asset_no_side")] = (
        "Hi.\n\n{asset} — bid or offer, size and rate?",
        "Hello.\n\n{asset}.\nDirection and numbers?",
        "Quick.\n\n{asset} — which side, clip, level?",
        "Hi.\n\n{asset} check.\nBuy/sell and price?",
        "Hello.\n\n{asset}.\nSide + size + rate?",
        "Hi.\n\n{asset} — what's your angle and quote?",
    )
    p[("en", "aggressive_trader", "asset_no_side")] = (
        "{asset}.\n\nBid or offer — size and level?",
        "{asset} — which way and at what price?",
        "On {asset}.\n\nNumbers?",
        "{asset} flow.\n\nClip + rate?",
        "{asset}.\n\nSide and quote?",
        "{asset} — go.",
    )

    p[("ru", "friendly", "asset_no_side")] = (
        "Привет, как дела?\n\nПо {asset}.\nПокупка или продажа у вас, объем и курс?",
        "Здравствуйте.\n\nКасательно {asset}.\nВаша сторона и цифры?",
        "Привет.\n\n{asset}.\nBid или offer — размер и цена?",
        "Добрый день.\n\nПо {asset}.\nНаправление, объем, курс?",
        "Привет.\n\n{asset} — какая сторона и какие уровни?",
        "Здравствуйте.\n\n{asset}.\nКак работаете — объем и цена?",
    )
    p[("ru", "neutral", "asset_no_side")] = p[("ru", "friendly", "asset_no_side")]
    p[("ru", "assertive", "asset_no_side")] = (
        "{asset}.\n\nПокупка/продажа — объем и курс?",
        "По {asset}.\n\nСторона и цифры?",
        "{asset}.\n\nBid или offer?",
        "Касательно {asset}.\n\nРазмер и цена?",
        "{asset}.\n\nНаправление и уровень?",
        "{asset}.\n\nКак котируете?",
    )
    p[("ru", "aggressive_trader", "asset_no_side")] = (
        "{asset}.\n\nСторона и level.",
        "{asset} — bid/offer, цифры.",
        "По {asset}.\n\nОбъем + курс.",
        "{asset}.\n\nКак работаете?",
        "{asset}.\n\nКлип и цена.",
        "{asset}.\n\nВ студию.",
    )

    p[("hy", "friendly", "asset_no_side")] = (
        "Բարև, ո՞նց եք։\n\n{asset}ի շուրջ։\nԳնո՞ւմ է, թե վաճառո՞ւմ — ծավալը և կուրսը։",
        "Բարև ձեզ։\n\n{asset} — ձեր կողմը և цифры։",
        "Բարև։\n\n{asset}։\nBid թե offer, ծավալ և գին։",
        "Բարև ձեզ։\n\nՀարց {asset}ով։\nՈւղղություն, ծավալ, կուրս։",
        "Բարև։\n\n{asset}։\nԻնչ ուղղությամբ եք աշխատում։",
        "Բարև ձեզ։\n\n{asset} — ասեք ծավալը և գինը։",
    )
    p[("hy", "neutral", "asset_no_side")] = p[("hy", "friendly", "asset_no_side")]
    p[("hy", "assertive", "asset_no_side")] = p[("hy", "friendly", "asset_no_side")]
    p[("hy", "aggressive_trader", "asset_no_side")] = (
        "{asset}։\n\nԿողմը և цифры։",
        "{asset} — bid/offer։",
        "Ուղղություն {asset}ով։\n\nՔանակ + կուրս։",
        "{asset}։\n\nԻնչ եք անում։",
        "{asset}։\n\nՏվյալներ։",
        "{asset}։\n\nԿարճ։",
    )

    # fallback — minimal facts
    p[("en", "friendly", "fallback")] = (
        "Hi, how are you?\n\nWalk me through what you're trying to do.\nWhat size, rate, and payment work for you?",
        "Hello.\n\nQuick intro — what trade are you working?\nSize, price, settlement?",
        "Hey.\n\nWhat's the desk looking for?\nAsset, volume, rate?",
        "Hi.\n\nNeed a bit of context.\nWhat leg, numbers, and how to settle?",
        "Good day.\n\nWhat are we pricing?\nSize, level, payment?",
        "Hi there.\n\nTell me the trade.\nVolume, rate, method?",
    )
    p[("en", "neutral", "fallback")] = (
        "Hello.\n\nPlease outline the trade.\nSize, rate, payment method?",
        "Hi.\n\nWhat instrument and terms?\nVolume, price, settlement?",
        "Good day.\n\nTrade details?\nAsset, size, rate?",
        "Hi.\n\nWhat do you need?\nNumbers and payment?",
        "Hello.\n\nDescribe the flow.\nSize, level, settlement?",
        "Hi.\n\nTrade inquiry.\nVolume, rate, payment?",
    )
    p[("en", "assertive", "fallback")] = (
        "Hi.\n\nWhat trade, size, and rate?",
        "Hello.\n\nAsset, volume, price — go.",
        "Quick.\n\nDetails: size, level, payment?",
        "Hi.\n\nWhat are we doing — numbers?",
        "Hello.\n\nTrade + settlement?",
        "Hi.\n\nSize, rate, method?",
    )
    p[("en", "aggressive_trader", "fallback")] = (
        "What trade?\n\nSize, level, payment.",
        "Go.\n\nAsset, clip, rate.",
        "Numbers?\n\nVolume + price + settlement.",
        "Trade.\n\nDetails.",
        "What leg?\n\nSize and rate.",
        "Quick.\n\nSize, price, how to pay.",
    )

    p[("ru", "friendly", "fallback")] = (
        "Привет, как дела?\n\nРасскажите, что нужно.\nОбъем, курс, способ расчета?",
        "Здравствуйте.\n\nКакая сделка?\nРазмер, цена, расчет?",
        "Привет.\n\nКонтекст сделки?\nОбъем, курс, оплата?",
        "Добрый день.\n\nЧто котируем?\nОбъем, уровень, расчет?",
        "Привет.\n\nНужны детали.\nАктив, объем, курс?",
        "Здравствуйте.\n\nОпишите запрос.\nЦифры и расчет?",
    )
    p[("ru", "neutral", "fallback")] = p[("ru", "friendly", "fallback")]
    p[("ru", "assertive", "fallback")] = (
        "Привет.\n\nСделка, объем, курс?",
        "Здравствуйте.\n\nАктив, размер, цена — вперед.",
        "Добрый день.\n\nДетали: объем, уровень, расчет?",
        "Привет.\n\nЧто делаем — цифры?",
        "Здравствуйте.\n\nСделка и расчет?",
        "Привет.\n\nОбъем, курс, способ?",
    )
    p[("ru", "aggressive_trader", "fallback")] = (
        "Сделка?\n\nОбъем, level, расчет.",
        "Вперед.\n\nАктив, клип, цена.",
        "Цифры?\n\nОбъем + курс + оплата.",
        "Что нужно?\n\nДетали.",
        "Какой leg?\n\nРазмер и цена.",
        "Быстро.\n\nОбъем, цена, расчет.",
    )

    p[("hy", "friendly", "fallback")] = (
        "Բարև, ո՞նց եք։\n\nԻնչ գործարք է պետք։\nծավալ, կուրս, վճարում։",
        "Բարև ձեզ։\n\nՆշեք առաջարկը։\nքանակ, գին, հաշվարկ։",
        "Բարև։\n\nԻնչ ակտիվ և ինչ պայմաններ։",
        "Բարև ձեզ։\n\nՏվյալներ գործարքի մասին։",
        "Բարև։\n\nՔանակ, կուրս, մեթոդ։",
        "Բարև ձեզ։\n\nԻնչ եք փնտրում։",
    )
    p[("hy", "neutral", "fallback")] = p[("hy", "friendly", "fallback")]
    p[("hy", "assertive", "fallback")] = p[("hy", "friendly", "fallback")]
    p[("hy", "aggressive_trader", "fallback")] = (
        "Ինչ trade։\n\nծավալ, level, վճարում։",
        "Առաջ։\n\nակտիվ, clip, գին։",
        "Տվյալներ։\n\nՔանակ + կուրս + հաշվարկ։",
        "Գործարք։\n\nԿարճ։",
        "Ինչ leg։\n\nՔանակ և գին։",
        "Արագ։\n\nծավալ, գին, վճարում։",
    )

    return p


_POOLS = _pools()

_BANNED_SUBSTRINGS = (
    "writing re:",
    "to move cleanly",
    "on our side",
    " that helps",
    "to keep this practical",
)


def assert_no_banned_opener_phrases(text: str) -> None:
    """Test helper: raises AssertionError if a banned substring appears."""
    low = text.lower()
    for b in _BANNED_SUBSTRINGS:
        if b.strip() in low or b in low:
            raise AssertionError(f"banned phrase {b!r} in opener")


def build_first_opener_message(
    *,
    task: Any,
    eff: dict[str, Any],
    goal: str,
    target: str,
    turn_number: int = 0,
) -> str:
    """Pick a stable human variant for the first outbound (OTC-safe)."""
    task_id = int(getattr(task, "id", 0) or 0)
    tone = getattr(task, "tone", None)
    lang_raw = getattr(task, "language", None)
    profile = map_task_tone_to_opener_profile(str(tone) if tone is not None else None)
    if profile not in _OPENER_PROFILES:
        profile = "neutral"
    lang = resolve_opener_language(str(lang_raw) if lang_raw is not None else None, goal)

    side = eff.get("side") if _eff_field_known(eff.get("side")) else None
    asset = eff.get("asset") if _eff_field_known(eff.get("asset")) else None
    side_l = str(side).strip().lower() if side else ""
    asset_s = str(asset).strip().upper() if asset else ""

    if side_l == "buy" and asset_s and _eff_field_known(eff.get("amount_crypto")):
        amt_raw = eff.get("amount_crypto")
        try:
            amt_f = float(amt_raw)
            amt_disp = str(int(amt_f)) if amt_f == int(amt_f) else str(amt_f)
        except (TypeError, ValueError):
            amt_disp = str(amt_raw or "").strip() or str(asset_s)
        net = (eff.get("payment_network") or "").strip().upper()
        via = f" via {net}" if net in ("ERC20", "TRC20", "BEP20") else ""
        lang_early = resolve_opener_language(str(lang_raw) if lang_raw is not None else None, goal)
        if lang_early == "ru":
            return (
                f"Здравствуйте.\n\nХочу купить {amt_disp} {asset_s}{via}.\n"
                f"Какой курс можете дать?"
            )
        if lang_early == "hy":
            return (
                f"Բարև։\n\nՈւզում եմ գնել {amt_disp} {asset_s}{via}։\n"
                f"Ո՞ր կուրսն եք առաջարկում։"
            )
        return (
            f"Hi, how are you?\n\n"
            f"I'm looking to buy {amt_disp} {asset_s}{via}.\n"
            f"What rate are you offering?"
        )

    if side_l == "buy" and asset_s:
        slot = "buy_asset"
    elif side_l == "sell" and asset_s:
        slot = "sell_asset"
    elif side_l == "buy":
        slot = "buy_no_asset"
    elif side_l == "sell":
        slot = "sell_no_asset"
    elif asset_s:
        slot = "asset_no_side"
    else:
        slot = "fallback"

    pool = _pick_pool(lang, profile, slot, _POOLS)
    idx = stable_opener_variant_index(
        task_id=task_id,
        target=target or "",
        goal=goal or "",
        turn_number=turn_number,
        lang=lang,
        profile=profile,
        pool_len=len(pool),
    )
    raw = pool[idx]
    text = _format_opener(raw, asset=asset_s)
    low = text.lower()
    if any(b in low for b in _BANNED_SUBSTRINGS):
        pool_fb = _pick_pool("en", "neutral", slot, _POOLS)
        i2 = stable_opener_variant_index(
            task_id=task_id,
            target=target or "",
            goal=(goal or "") + ":fb",
            turn_number=turn_number,
            lang="en",
            profile="neutral",
            pool_len=len(pool_fb),
        )
        text = _format_opener(pool_fb[i2], asset=asset_s)
    return text
