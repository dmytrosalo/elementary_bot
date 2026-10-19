"""Телеграм-бот складу Vatra Bistro.

Показує залишки з Elementary POS. Кухонні позиції (SKU `SK-`) окремо від
бару, бо кухарів цікавлять лише свої одинадцять, а бар — це ще сорок рядків.

Працює довгим опитуванням: вихідних з'єднань достатньо, вебхук вимагав би
домену й місця в Caddy заради двох команд. Залежностей немає навмисно —
коробка вже тримає `vatra-subscribe` так само, на голій стандартній бібліотеці.
"""

import html
import json
import re
import os
import sys
import time
import urllib.error
import urllib.request

TG = "https://api.telegram.org/bot{}/{}"
POS = "https://api.elementarypos.com"

KUHNYA = "SK-"          # префікс SKU, за яким відрізняємо кухню від бару
ODYNYCI = {"ks", "kg", "l", "pc", "g", "ml", "cl", "dag", "porce"}

# Тара стоїть у назві: «Doppio Passo Primitivo [0,75 l]». Для штучних позицій
# каса дужок не має, зате розмір є у хвості: «Coca-Cola Classic 0,33 l».
# «[0,75 l]», «[keg 50 l]», «[13 ks/porce]» — хвіст після скісної лише підпис.
TARA = re.compile(r"\[(?:keg\s*)?(\d+(?:[.,]\d+)?)\s*(l|ml|kg|g|ks|pc)(?:\s*/\s*[^\]]+)?\]")
ROZMIR = re.compile(r"(\d+(?:[.,]\d+)?)\s*(l|ml)\s*$")

DOVIDKA = (
    "Я показую залишки складу з каси.\n\n"
    "/stock — кухня\n"
    "/stock bar — бар і склад\n"
    "/stock all — усе разом"
)


def env(nazva, typovo=None):
    znachennya = os.environ.get(nazva, typovo)
    if znachennya is None:
        sys.exit("нема змінної оточення " + nazva)
    return znachennya


def zapyt(url, dani=None, zagolovky=None, chas=65):
    tilo = json.dumps(dani).encode() if dani is not None else None
    req = urllib.request.Request(url, data=tilo, headers=zagolovky or {})
    if tilo is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=chas) as vidpovid:
        return json.load(vidpovid)


def tg(metod, **params):
    return zapyt(TG.format(TOKEN, metod), params)


def sklad():
    return zapyt(
        POS + "/v1/stock/get-actual-stock-data", {}, {"X-Api-Key": KEY}, 30
    )["list"]


def nazva(pozyciya):
    """Прибрати хвіст «(ks)» і тару — обоє вже є в колонках зліва."""
    text = (pozyciya.get("item") or pozyciya.get("sku") or "").strip()
    text = TARA.sub("", text).strip()
    if text.endswith(")"):
        pochatok = text.rfind("(")
        if pochatok > 0 and text[pochatok + 1:-1].strip().lower() in ODYNYCI:
            text = text[:pochatok].strip()
    return text


def chyslo(kilkist):
    """Ціле — без хвоста, дробове — без зайвих нулів: 84, 0.76, -0.3."""
    okruhleno = round(float(kilkist), 2)
    if okruhleno == int(okruhleno):
        return str(int(okruhleno))
    return ("%.2f" % okruhleno).rstrip("0")


def tara(pozyciya):
    """Об'єм однієї тари в основній одиниці каси, або None якщо не вказано."""
    text = pozyciya.get("item") or ""
    znayshov = TARA.search(text) or ROZMIR.search(text)
    if not znayshov:
        return None
    obsyag = float(znayshov.group(1).replace(",", "."))
    odynycya = znayshov.group(2)
    if odynycya == "ml":
        obsyag /= 1000
    if odynycya == "g":
        obsyag /= 1000
    return obsyag or None


def obydvi(pozyciya):
    """Літри й пляшки тієї самої позиції, незалежно від того, що тримає каса.

    Каса рахує щось одне: сиропи в літрах, коли в штуках. Але дивляться на
    обидва — літри сходяться зі списанням, пляшки з полицею й замовленням.
    Чого порахувати не вдалося (тари в назві немає), повертаємо None.
    """
    obsyag = tara(pozyciya)
    kilkist = float(pozyciya["quantity"])
    if pozyciya.get("unit") == "pc":
        return (round(kilkist * obsyag, 2) if obsyag else None), kilkist
    return kilkist, (round(kilkist / obsyag, 1) if obsyag else None)


def kuhonna_odynycya(pozyciya):
    """У касі все в pc, але борщ рахують порціями, а вареники штуками."""
    return "порц" if "porce" in (pozyciya.get("item") or "").lower() else "шт"


def malo(pozyciya):
    """Поріг живе в касі, не тут: там його видно й міняє будь-хто."""
    return bool(pozyciya.get("lowQuantity")) or float(pozyciya["quantity"]) <= 0


def stovpec(znachennya):
    return "—" if znachennya is None else chyslo(znachennya)


def tablycya(zagolovok, pidpysy, pozyciyi, para_f, za_paroyu=False):
    """Сталі колонки з підписом угорі: що в колонці — видно раз, не в рядку.

    Друга колонка з'являється лише якщо її є чим наповнити: суцільний стовпчик
    прочерків нічого не каже, тільки збиває.
    """
    if not pozyciyi:
        return []
    pary = [(p, para_f(p)) for p in pozyciyi]
    # Кухню міряють порціями, а не штуками: 39 вареників — це три порції,
    # тобто майже кінець, тоді як 40 пампушок — двадцять. Сортуємо по тому,
    # чим міряють, інакше найближче до нуля ховається в середині списку.
    def klyuch(t):
        persha, druha = t[1]
        if za_paroyu and druha is not None:
            return druha
        return persha if persha is not None else float(t[0]["quantity"])

    pary.sort(key=klyuch)
    para_potribna = any(druha is not None for _, (_, druha) in pary)

    ryadky = [zagolovok, ("%7s %6s" % pidpysy) if para_potribna else ("%7s" % pidpysy[0])]
    for p, (persha, druha) in pary:
        if para_potribna:
            pochatok = "%7s %6s  " % (stovpec(persha), stovpec(druha))
        else:
            pochatok = "%7s  " % stovpec(persha)
        ryadky.append((pochatok + nazva(p) + ("  !" if malo(p) else "")).rstrip())
    return ryadky


def kuhonna_para(pozyciya):
    """Друга колонка кухні — порції, якщо в назві вказано, скільки штук у порції."""
    u_porciyi = tara(pozyciya)
    kilkist = float(pozyciya["quantity"])
    return kilkist, (round(kilkist / u_porciyi, 1) if u_porciyi else None)


def vagova_para(pozyciya):
    obsyag = tara(pozyciya)
    kilkist = float(pozyciya["quantity"])
    return kilkist, (round(kilkist / obsyag, 1) if obsyag else None)


def povidomlennya(rozriz):
    pozyciyi = sklad()
    kuhnya = [p for p in pozyciyi if (p.get("sku") or "").startswith(KUHNYA)]
    reshta = [p for p in pozyciyi if not (p.get("sku") or "").startswith(KUHNYA)]

    # Три таблиці, бо три різні міри. Змішувати їх в одній колонці не можна:
    # «0.7» біля «27» читалося б як одне й те саме, а це літр і пляшка.
    vaga = [p for p in reshta if p.get("unit") == "kg"]
    ridke = [p for p in reshta if p.get("unit") == "l" or (p.get("unit") == "pc" and tara(p))]
    shtuchne = [p for p in reshta if p not in vaga and p not in ridke]

    ryadky = ["Склад · " + time.strftime("%d.%m %H:%M"), ""]
    kuhonna = tablycya("Кухня", ("шт", "порц"), kuhnya, kuhonna_para, za_paroyu=True)
    barna = (
        tablycya("Бар і склад", ("л", "пл"), ridke, obydvi)
        + ([""] + tablycya("Вага", ("кг", "пач"), vaga, vagova_para) if vaga else [])
        + ([""] + tablycya("Поштучно", ("шт", "порц"), shtuchne, kuhonna_para) if shtuchne else [])
    )

    if rozriz in ("", "kuhnya"):
        ryadky += kuhonna + ["", "Бар і склад: /stock bar"]
    elif rozriz == "bar":
        ryadky += barna + ["", "Кухня: /stock"]
    else:
        ryadky += kuhonna + [""] + barna
    return "<pre>" + html.escape("\n".join(ryadky)) + "</pre>"


def vidpovisty(chat, text):
    tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML")


def obrobyty(povid):
    text = (povid.get("text") or "").strip()
    chat = povid["chat"]["id"]
    if not text.startswith("/"):
        return

    slova = text.split()
    komanda = slova[0].split("@")[0].lower()

    # Id у журнал: саме його треба буде вписати, коли чат звузимо до свого.
    print("чат %d (%s): %s" % (chat, povid["chat"].get("title") or "особистий", komanda), flush=True)

    if not VSIM and chat not in CHATY:
        # Ім'я бота не секрет, тож чужий чат мусить впертися в стіну. Порожній
        # список — теж стіна, інакше свіжий бот стояв би відчинений. Id кажемо:
        # саме його треба вписати в налаштування, щоб чат пустили.
        vidpovisty(chat, "Цей чат не дозволений.\nЙого id: <code>%d</code>" % chat)
        return

    if komanda in ("/start", "/help"):
        vidpovisty(chat, DOVIDKA)
    elif komanda == "/stock":
        rozriz = slova[1].lower() if len(slova) > 1 else ""
        if rozriz not in ("", "bar", "all", "kuhnya"):
            vidpovisty(chat, DOVIDKA)
            return
        try:
            vidpovisty(chat, povidomlennya(rozriz))
        except Exception as pomylka:               # каса може лежати або тягнути
            print("каса не відповіла:", pomylka, flush=True)
            vidpovisty(chat, "Каса не відповідає. Спробуй за хвилину.")


def main():
    if "--stock" in sys.argv:                      # перевірка ключа без телеграма
        print(povidomlennya("all").replace("<pre>", "").replace("</pre>", ""))
        return

    print("бот запущений, %s" % ("відчинено всім" if VSIM else "чатів у списку: %d" % len(CHATY)), flush=True)
    zsuv = 0
    while True:
        try:
            onovlennya = tg("getUpdates", offset=zsuv, timeout=50)["result"]
        except Exception as pomylka:
            print("getUpdates:", pomylka, flush=True)
            time.sleep(5)
            continue
        for o in onovlennya:
            zsuv = o["update_id"] + 1              # підтверджуємо навіть те, що впало
            try:
                povid = o.get("message") or o.get("channel_post")
                if povid:
                    obrobyty(povid)
            except Exception as pomylka:
                print("обробка:", pomylka, flush=True)


TOKEN = env("TELEGRAM_BOT_TOKEN")
KEY = env("ELEMENTARY_API_KEY")
# `all` — відчинено всім навмисно; порожньо — зачинено всім. Обидва стани
# явні, бо мовчазне «порожньо значить заходь» одного разу вже було помилкою.
_SPYSOK = env("ALLOWED_CHAT_IDS", "").replace(",", " ").split()
VSIM = _SPYSOK == ["all"]
CHATY = set() if VSIM else {int(c) for c in _SPYSOK}

if __name__ == "__main__":
    main()
