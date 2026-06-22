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
import audiobusio
import audiocore
import audiomixer
from adafruit_display_text import label
from adafruit_bitmap_font import bitmap_font
from adafruit_datetime import datetime, timedelta
from adafruit_ticks import ticks_ms, ticks_add, ticks_diff

SMALL_FONT = bitmap_font.load_font("/tom-thumb.bdf")

# ── I2S Audio ─────────────────────────────────────────────────────────────────
# MAX98357A: BCLK→A3, LRC→A4, DIN→TX
# WAV files: 16kHz mono 16-bit PCM, stored in /sounds/
# Volume: 0.0 = silent, 1.0 = full blast
VOLUME = 0.01

try:
    i2s = audiobusio.I2SOut(board.A3, board.A4, board.TX)
    mixer = audiomixer.Mixer(
        voice_count=1,
        sample_rate=16000,
        channel_count=1,
        bits_per_sample=16,
        samples_signed=True,
    )
    mixer.voice[0].level = VOLUME
    i2s.play(mixer)
    HAS_AUDIO = True
except Exception as _audio_err:
    print("I2S INIT ERROR:", repr(_audio_err))
    i2s = None
    mixer = None
    HAS_AUDIO = False


def play_sound(filepath):
    if not HAS_AUDIO or mixer is None:
        return
    try:
        with open(filepath, "rb") as f:
            wav = audiocore.WaveFile(f)
            mixer.voice[0].play(wav)
            while mixer.voice[0].playing:
                pass
    except Exception as e:
        print("AUDIO ERROR:", repr(e))
TIMEZONE_LABEL = "ET"
TIMEZONE_OFFSET = -4  # EDT (UTC-4); change to -5 for EST

FETCH_SECONDS = 120
DISPLAY_SECONDS = 8

SPORT_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"

WHITE = 0x666666   # main text: teams, VS, scores
DIM = 0x333333     # secondary text: time, ET, city
RED = 0xFF0000     # errors only
GOLD = 0xFFAA00    # goal flash

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
    bit_depth=6,
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
display.brightness = 0.3

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
    return str(city).upper()


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


def parse_color(hex_str, fallback=None):
    if fallback is None:
        fallback = WHITE
    if not hex_str:
        return fallback
    try:
        return int(str(hex_str).lstrip("#"), 16)
    except (ValueError, TypeError):
        return fallback


def sanitize_color(color):
    r = (color >> 16) & 0xFF
    g = (color >> 8) & 0xFF
    b = color & 0xFF
    if max(r, g, b) < 0x20:
        return WHITE
    return color


def format_ml(value):
    if value is None:
        return "---"
    try:
        n = int(value)
        return "+{}".format(n) if n > 0 else str(n)
    except (ValueError, TypeError):
        return "---"


def get_team_colors(competitor):
    team = competitor.get("team", {}) if competitor else {}
    primary = sanitize_color(parse_color(team.get("color")))
    alt = sanitize_color(parse_color(team.get("alternateColor")))
    return primary, alt


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

    home_color, home_alt = get_team_colors(home)
    away_color, away_alt = get_team_colors(away)

    odds_list = competition.get("odds", [])
    odds = odds_list[0] if odds_list else {}
    if odds is None:
        odds = {}
    ml = odds.get("moneyline") or {}
    home_ml = (ml.get("home") or {}).get("close", {}).get("odds")
    draw_ml = (ml.get("draw") or {}).get("close", {}).get("odds")
    away_ml = (ml.get("away") or {}).get("close", {}).get("odds")

    home_team_id = (home.get("team", {}) if home else {}).get("id", "")
    away_team_id = (away.get("team", {}) if away else {}).get("id", "")

    return {
        "event_id": event.get("id", ""),
        "home": get_team_abbrev(home),
        "away": get_team_abbrev(away),
        "home_team_id": home_team_id,
        "away_team_id": away_team_id,
        "home_color": home_color,
        "home_alt_color": home_alt,
        "away_color": away_color,
        "away_alt_color": away_alt,
        "home_score": home.get("score", "") if home else "",
        "away_score": away.get("score", "") if away else "",
        "date": convert_game_time(event.get("date", "")),
        "clock": status.get("displayClock", ""),
        "status_name": status_type.get("name", ""),
        "status_state": status_type.get("state", ""),
        "status_detail": status_type.get("detail", ""),
        "status_short": status_type.get("shortDetail", ""),
        "city": fit_city(city),
        "home_ml": home_ml,
        "draw_ml": draw_ml,
        "away_ml": away_ml,
        "details": competition.get("details", []),
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


def make_colored_abbrev(abbrev, x, y, color1, color2):
    # terminalio.FONT is 6px wide per character
    # 3 letters centered at x: offsets are -6, 0, +6
    colors = [color1, color2, color1]
    offsets = [-6, 0, 6]
    labels = []
    for i, ch in enumerate(abbrev[:3]):
        lbl = label.Label(terminalio.FONT, text=ch, color=colors[i])
        lbl.anchor_point = (0.5, 0.5)
        lbl.anchored_position = (x + offsets[i], y)
        labels.append(lbl)
    return labels


def build_score_cache(games):
    cache = {}
    for game in games:
        event_id = game["event_id"]
        if not event_id:
            continue
        try:
            home_score = int(game["home_score"])
            away_score = int(game["away_score"])
            cache[event_id] = (home_score, away_score)
        except (ValueError, TypeError):
            pass
    return cache


def check_for_goals(score_cache, new_games):
    goals = []
    for game in new_games:
        event_id = game["event_id"]
        if not event_id or event_id not in score_cache:
            continue
        try:
            new_home = int(game["home_score"])
            new_away = int(game["away_score"])
        except (ValueError, TypeError):
            continue
        old_home, old_away = score_cache[event_id]
        if new_home > old_home:
            goals.append((game, "home"))
        if new_away > old_away:
            goals.append((game, "away"))
    return goals


def load_flag(abbrev):
    """Load a flag BMP and return (bitmap, palette) or (None, None) on failure."""
    try:
        bmp, pal = adafruit_imageload.load(
            "/team_logos/{}.bmp".format(abbrev),
            bitmap=displayio.Bitmap,
            palette=displayio.Palette,
        )
        pal.make_opaque(0)
        return bmp, pal
    except Exception as e:
        print("FLAG LOAD ERROR:", repr(e))
        return None, None


def make_goal_group(game, scorer):
    group = displayio.Group()
    if scorer == "home":
        abbrev = game["home"]
        color1 = game["home_color"]
        color2 = game["home_alt_color"]
    else:
        abbrev = game["away"]
        color1 = game["away_color"]
        color2 = game["away_alt_color"]

    bmp, pal = load_flag(abbrev)
    if bmp is not None:
        group.append(displayio.TileGrid(bmp, pixel_shader=pal, x=0, y=0))
        for lbl in make_colored_abbrev(abbrev, 48, 10, color1, color2):
            group.append(lbl)
        group.append(make_label("GOAL!", 48, 22, GOLD))
    else:
        for lbl in make_colored_abbrev(abbrev, 32, 11, color1, color2):
            group.append(lbl)
        group.append(make_label("GOAL!", 32, 22, GOLD))

    return group


def build_card_cache(games):
    cache = {}
    for game in games:
        event_id = game["event_id"]
        if not event_id:
            continue
        fingerprints = set()
        for detail in game.get("details", []):
            try:
                clock_val = detail.get("clock", {}).get("value")
                team_id = detail.get("team", {}).get("id")
                type_id = detail.get("type", {}).get("id")
                if clock_val is not None and team_id and type_id:
                    fingerprints.add((clock_val, team_id, type_id))
            except Exception:
                pass
        cache[event_id] = fingerprints
    return cache


def check_for_cards(card_cache, new_games):
    cards = []
    for game in new_games:
        event_id = game["event_id"]
        if not event_id:
            continue
        old_prints = card_cache.get(event_id, set())
        for detail in game.get("details", []):
            if not (detail.get("yellowCard") or detail.get("redCard")):
                continue
            try:
                clock_val = detail.get("clock", {}).get("value")
                team_id = detail.get("team", {}).get("id")
                type_id = detail.get("type", {}).get("id")
                if clock_val is None or not team_id or not type_id:
                    continue
                fp = (clock_val, team_id, type_id)
                if fp not in old_prints:
                    card_type = "red" if detail.get("redCard") else "yellow"
                    cards.append((game, detail, card_type))
            except Exception:
                pass
    return cards


def make_card_group(game, detail, card_type):
    group = displayio.Group()

    # Resolve which team got the card
    card_team_id = detail.get("team", {}).get("id", "")
    if card_team_id == game["home_team_id"]:
        abbrev = game["home"]
        color1 = game["home_color"]
        color2 = game["home_alt_color"]
    else:
        abbrev = game["away"]
        color1 = game["away_color"]
        color2 = game["away_alt_color"]

    # Card label text and color
    if card_type == "red":
        card_text = "RED"
        card_color = RED
    else:
        card_text = "YELLOW"
        card_color = 0xFFFF00

    # Player position (optional bottom label)
    athletes = detail.get("athletesInvolved", [])
    position = athletes[0].get("position") if athletes else None

    bmp, pal = load_flag(abbrev)
    if bmp is not None:
        group.append(displayio.TileGrid(bmp, pixel_shader=pal, x=0, y=0))
        group.append(make_label(card_text, 48, 10, card_color))
        if position:
            group.append(make_label(position, 48, 22, DIM))
    else:
        group.append(make_label(card_text, 32, 11, card_color))
        if position:
            group.append(make_label(position, 32, 22, DIM))

    return group


def make_small_label(text, x, y, color=WHITE):
    lbl = label.Label(SMALL_FONT, text=str(text), color=color)
    lbl.anchor_point = (0.5, 0.5)
    lbl.anchored_position = (x, y)
    return lbl

def make_odds_group(game):
    group = displayio.Group()

    # Row 1: identical to pre-game — HOME VS AWAY
    for lbl in make_colored_abbrev(game["home"], 12, 5, game["home_color"], game["home_alt_color"]):
        group.append(lbl)
    group.append(make_label("VS", 32, 5, WHITE))
    for lbl in make_colored_abbrev(game["away"], 52, 5, game["away_color"], game["away_alt_color"]):
        group.append(lbl)

    # Row 2: identical to pre-game — game time
    group.append(make_label("{} {}".format(game["date"], TIMEZONE_LABEL), 32, 16, DIM))

    # Row 3: moneyline odds in tom-thumb (H / D / A) — replaces city
    group.append(make_small_label(format_ml(game["home_ml"]), 11, 27, WHITE))
    group.append(make_small_label(format_ml(game["draw_ml"]), 32, 27, DIM))
    group.append(make_small_label(format_ml(game["away_ml"]), 53, 27, WHITE))

    return group


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
    for lbl in make_colored_abbrev(game["home"], 12, 5, game["home_color"], game["home_alt_color"]):
        group.append(lbl)
    group.append(make_label(fit_text(center, 7), 32, 5, WHITE))
    for lbl in make_colored_abbrev(game["away"], 52, 5, game["away_color"], game["away_alt_color"]):
        group.append(lbl)

    # Middle row: time, clock, or FINAL
    group.append(make_label(fit_text(middle, 10), 32, 16, DIM))

    # Bottom row: city
    if bottom:
        group.append(make_small_label(fit_text(bottom, 16), 32, 27, DIM))

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
        home_score = str(game["home_score"])
        away_score = str(game["away_score"])
        is_pre = (
            game["status_state"] == "pre"
            or home_score == ""
            or away_score == ""
        )
        if is_pre and (game["home_ml"] or game["draw_ml"] or game["away_ml"]):
            groups.append(make_odds_group(game))
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
    score_cache = build_score_cache(games)
    card_cache = build_card_cache(games)

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
            new_games = fetch_games()

            # check for goals and cards before rebuilding groups
            goals = check_for_goals(score_cache, new_games)
            for goal_game, scorer in goals:
                display.root_group = make_goal_group(goal_game, scorer)
                play_sound("/sounds/goal.wav")
                time.sleep(10)

            cards = check_for_cards(card_cache, new_games)
            for card_game, detail, card_type in cards:
                display.root_group = make_card_group(card_game, detail, card_type)
                time.sleep(10)

            games = new_games
            score_cache = build_score_cache(games)
            card_cache = build_card_cache(games)

            groups = make_groups(games)
            if HAS_LOGO:
                try:
                    groups = [make_logo_group()] + groups
                except Exception as error:
                    print("LOGO GROUP ERROR:", repr(error))

            display_index = 0
            display.root_group = groups[0]
            display_clock = ticks_ms()
            fetch_clock = ticks_add(fetch_clock, FETCH_SECONDS * 1000)
        except Exception as error:
            print("REFRESH ERROR:", repr(error))

    time.sleep(0.1)
