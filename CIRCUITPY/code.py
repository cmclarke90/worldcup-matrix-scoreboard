# Single-panel World Cup scoreboard for Matrix Portal S3 + one 64x32 RGB matrix
# Save as CIRCUITPY/code.py

import os
import gc
import ssl
import time
import wifi
import socketpool
import board
import displayio
import framebufferio
import rgbmatrix
import terminalio
import neopixel
import adafruit_requests
import adafruit_imageload
from adafruit_display_text import label
from adafruit_datetime import datetime, timedelta
from adafruit_ticks import ticks_ms, ticks_add, ticks_diff


TIMEZONE_OFFSET = -4
TIMEZONE_LABEL = "ET"

FETCH_SECONDS = 300
DISPLAY_SECONDS = 12

SPORT_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"

WHITE = 0x666666   # main text: teams, VS, scores
DIM = 0x333333     # secondary text: time, ET, city
RED = 0xFF0000     # errors only

try:
    LOGO_BITMAP, LOGO_PALETTE = adafruit_imageload.load(
        "/worldcup.bmp",
        bitmap=displayio.Bitmap,
        palette=displayio.Palette,
    )
    HAS_LOGO = True
except Exception as error:
    print("LOGO ERROR:", repr(error))
    HAS_LOGO = False

displayio.release_displays()

matrix = rgbmatrix.RGBMatrix(
    width=64,
    height=32,
    bit_depth=4,
    rgb_pins=[
        board.MTX_R1,
        board.MTX_G1,
        board.MTX_B1,
        board.MTX_R2,
        board.MTX_G2,
        board.MTX_B2,
    ],
    addr_pins=[
        board.MTX_ADDRA,
        board.MTX_ADDRB,
        board.MTX_ADDRC,
        board.MTX_ADDRD,
    ],
    clock_pin=board.MTX_CLK,
    latch_pin=board.MTX_LAT,
    output_enable_pin=board.MTX_OE,
)

display = framebufferio.FramebufferDisplay(matrix, auto_refresh=True, rotation=0)
display.brightness = 0.05

pixel = neopixel.NeoPixel(board.NEOPIXEL, 1, brightness=0.25, auto_write=True)


def fit_text(text, max_chars):
    if text is None:
        return ""
    text = str(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars]

def fit_city(city):
    if not city:
        return ""

    city = str(city)

    city_map = {
        "Mexico City": "MEX CITY",
        "Guadalajara": "GUAD",
        "Monterrey": "MTY",
        "Los Angeles": "LA",
        "Santa Clara": "S CLARA",
        "Seattle": "SEATTLE",
        "Vancouver": "VANCOUVER",
        "Toronto": "TORONTO",
        "Kansas City": "KC",
        "Houston": "HOUSTON",
        "Dallas": "DALLAS",
        "Atlanta": "ATLANTA",
        "Miami": "MIAMI",
        "Boston": "BOSTON",
        "Philadelphia": "PHILLY",
        "East Rutherford": "NJ",
    }

    if city in city_map:
        return city_map[city]

    return fit_text(city.upper(), 10)


def make_label(text, x, y, color=WHITE):
    text_area = label.Label(terminalio.FONT, text=str(text), color=color)
    text_area.anchor_point = (0.5, 0.5)
    text_area.anchored_position = (x, y)
    return text_area


def show_message(line1, line2="", color=WHITE):
    group = displayio.Group()
    group.append(make_label(fit_text(line1, 10), 32, 10, color))
    if line2:
        group.append(make_label(fit_text(line2, 10), 32, 23, WHITE))
    display.root_group = group

def make_logo_group():
    group = displayio.Group()

    if HAS_LOGO:
        logo = displayio.TileGrid(
            LOGO_BITMAP,
            pixel_shader=LOGO_PALETTE,
            x=16,
            y=0,
        )
        group.append(logo)
    else:
        group.append(make_label("WORLD", 32, 10, WHITE))
        group.append(make_label("CUP", 32, 23, DIM))

    return group

def convert_game_time(date_string):
    try:
        year = int(date_string[0:4])
        month = int(date_string[5:7])
        day = int(date_string[8:10])
        hour = int(date_string[11:13])
        minute = int(date_string[14:16])

        dt = datetime(year, month, day, hour, minute)
        local_dt = dt + timedelta(hours=TIMEZONE_OFFSET)

        hour_12 = local_dt.hour % 12
        if hour_12 == 0:
            hour_12 = 12

        am_pm = "A" if local_dt.hour < 12 else "P"
        return "{}:{:02d}{}".format(hour_12, local_dt.minute, am_pm)

    except Exception:
        return "TIME?"


def get_team_abbrev(competitor):
    if not competitor:
        return "TBD"

    team = competitor.get("team", {})
    return fit_text(
        team.get("abbreviation")
        or team.get("shortDisplayName")
        or team.get("displayName")
        or "TBD",
        3,
    )


def parse_event(event):
    competition = event.get("competitions", [{}])[0]
    competitors = competition.get("competitors", [])

    home = None
    away = None

    for competitor in competitors:
        if competitor.get("homeAway") == "home":
            home = competitor
        elif competitor.get("homeAway") == "away":
            away = competitor

    if home is None and len(competitors) > 0:
        home = competitors[0]
    if away is None and len(competitors) > 1:
        away = competitors[1]

    status = event.get("status", {})
    status_type = status.get("type", {})

    venue = competition.get("venue", {})
    address = venue.get("address", {})
    city = address.get("city", "")

    return {
        "home": get_team_abbrev(home),
        "away": get_team_abbrev(away),
        "home_score": home.get("score", "") if home else "",
        "away_score": away.get("score", "") if away else "",
        "date": convert_game_time(event.get("date", "")),
        "clock": status.get("displayClock", ""),
        "status_name": status_type.get("name", ""),
        "status_state": status_type.get("state", ""),
        "status_detail": status_type.get("detail", ""),
        "status_short": status_type.get("shortDetail", ""),
        "city": fit_city(city),
    }


def fetch_games():
    pixel.fill((0, 0, 40))
    response = requests.get(SPORT_URL)
    data = response.json()
    response.close()

    games = []
    for event in data.get("events", []):
        games.append(parse_event(event))

    pixel.fill((0, 40, 0))
    return games


def make_game_group(game):
    group = displayio.Group()

    status_state = game["status_state"]
    status_name = game["status_name"]
    status_short = game["status_short"]
    status_detail = game["status_detail"]

    home_score = str(game["home_score"])
    away_score = str(game["away_score"])

    is_scheduled = (
        status_state == "pre"
        or status_name == "STATUS_SCHEDULED"
        or home_score == ""
        or away_score == ""
        or "scheduled" in status_short.lower()
        or "scheduled" in status_detail.lower()
    )

    is_final = (
        status_state == "post"
        or status_name == "STATUS_FINAL"
        or "final" in status_short.lower()
        or "final" in status_detail.lower()
    )

    if is_scheduled:
        center = "VS"
        middle = "{} {}".format(game["date"], TIMEZONE_LABEL)
        bottom = game["city"]

    else:
        center = "{}-{}".format(home_score, away_score)

        if is_final:
            middle = "FINAL"
        elif game["clock"]:
            middle = fit_text(game["clock"], 10)
        else:
            middle = fit_text(status_short or status_detail, 10)

        bottom = game["city"]

    # Top row: country / score-or-VS / country
    group.append(make_label(game["home"], 12, 5, WHITE))
    group.append(make_label(fit_text(center, 7), 32, 5, WHITE))
    group.append(make_label(game["away"], 52, 5, WHITE))

    # Middle row: time, clock, or FINAL
    group.append(make_label(fit_text(middle, 10), 32, 16, DIM))

    # Bottom row: city
    if bottom:
        group.append(make_label(fit_text(bottom, 10), 32, 27, DIM))

    return group


def make_groups(games):
    if not games:
        group = displayio.Group()
        group.append(make_label("NO GAMES", 32, 10, WHITE))
        group.append(make_label("FOUND", 32, 23, WHITE))
        return [group]

    groups = []
    for game in games:
        groups.append(make_game_group(game))
    return groups


show_message("WIFI", "CONNECT")
pixel.fill((40, 40, 0))

try:
    ssid = os.getenv("CIRCUITPY_WIFI_SSID")
    password = os.getenv("CIRCUITPY_WIFI_PASSWORD")
    wifi.radio.connect(ssid, password)

except Exception as error:
    print("WIFI ERROR:", repr(error))
    pixel.fill((40, 0, 0))
    show_message("WIFI ERR", "CHECK TOML", RED)
    while True:
        time.sleep(1)

pool = socketpool.SocketPool(wifi.radio)
requests = adafruit_requests.Session(pool, ssl.create_default_context())

try:
    show_message("LOADING", "SCORES")
    games = fetch_games()
    groups = make_groups(games)

    if HAS_LOGO:
        try:
            groups = [make_logo_group()] + groups
        except Exception as error:
            print("LOGO GROUP ERROR:", repr(error))

    display.root_group = groups[0]

except Exception as error:
    print("API ERROR:", repr(error))
    pixel.fill((40, 0, 0))
    show_message("API ERR", "MU LOG", RED)
    while True:
        time.sleep(1)

fetch_clock = ticks_ms()
display_clock = ticks_ms()
display_index = 0

while True:
    now = ticks_ms()

    if ticks_diff(now, display_clock) >= DISPLAY_SECONDS * 1000:
        display_index = (display_index + 1) % len(groups)
        display.root_group = groups[display_index]
        display_clock = ticks_add(display_clock, DISPLAY_SECONDS * 1000)

    if ticks_diff(now, fetch_clock) >= FETCH_SECONDS * 1000:
        try:
            gc.collect()
            games = fetch_games()
            groups = make_groups(games)
            if HAS_LOGO:
                try:
                    groups = [make_logo_group()] + groups
                except Exception as error:
                    print("LOGO GROUP ERROR:", repr(error))

            display_index = 0
            display.root_group = groups[0]
            fetch_clock = ticks_add(fetch_clock, FETCH_SECONDS * 1000)
        except Exception as error:
            print("REFRESH ERROR:", repr(error))

    time.sleep(0.1)
