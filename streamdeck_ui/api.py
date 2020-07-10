"""Defines the Python API for interacting with the StreamDeck Configuration UI"""
import json
import os
import time
import threading
import itertools
from functools import partial
from subprocess import Popen  # nosec - Need to allow users to specify arbitrary commands
from datetime import datetime
from typing import Dict, List, Tuple, Union
from warnings import warn

from PIL import Image, ImageSequence, ImageDraw, ImageFont
from pynput.keyboard import Controller, Key
from StreamDeck import DeviceManager, ImageHelpers
from StreamDeck.Devices import StreamDeck
from StreamDeck.ImageHelpers import PILHelper

from streamdeck_ui.config import CONFIG_FILE_VERSION, DEFAULT_FONT, FONTS_PATH, STATE_FILE

image_cache: Dict[str, Union[memoryview, Union[float, Union[int, int]]]] = {}
decks: Dict[str, StreamDeck.StreamDeck] = {}
state: Dict[str, Dict[str, Union[int, Dict[int, Dict[int, Dict[str, str]]]]]] = {}

pill2kill = threading.Event()
render_threads = []
FRAMES_PER_SECOND = 60
lock = threading.Lock()

def _key_change_callback(deck_id: str, _deck: StreamDeck.StreamDeck, key: int, state: bool) -> None:
    if state:
        keyboard = Controller()
        page = get_page(deck_id)

        command = get_button_command(deck_id, page, key)
        if command:
            Popen(command.split(" "))

        keys = get_button_keys(deck_id, page, key)
        if keys:
            keys = keys.strip().replace(" ", "")
            for section in keys.split(","):
                for key_name in section.split("+"):
                    keyboard.press(getattr(Key, key_name.lower(), key_name))
                for key_name in section.split("+"):
                    keyboard.release(getattr(Key, key_name.lower(), key_name))

        write = get_button_write(deck_id, page, key)
        if write:
            keyboard.type(write)

        brightness_change = get_button_change_brightness(deck_id, page, key)
        if brightness_change:
            change_brightness(deck_id, brightness_change)

        switch_page = get_button_switch_page(deck_id, page, key)
        if switch_page:
            set_page(deck_id, switch_page - 1)


def _save_state():
    export_config(STATE_FILE)


def _open_config(config_file: str):
    global state

    with open(config_file) as state_file:
        config = json.loads(state_file.read())
        file_version = config.get("streamdeck_ui_version", 0)
        if file_version != CONFIG_FILE_VERSION:
            raise ValueError(
                "Incompatible version of config file found: "
                f"{file_version} does not match required version "
                f"{CONFIG_FILE_VERSION}."
            )

        state = {}
        for deck_id, deck in config["state"].items():
            deck["buttons"] = {
                int(page_id): {int(button_id): button for button_id, button in buttons.items()}
                for page_id, buttons in deck.get("buttons", {}).items()
            }
            state[deck_id] = deck


def import_config(config_file: str) -> None:
    _open_config(config_file)
    render()
    _save_state()


def export_config(output_file: str) -> None:
    with open(output_file, "w") as state_file:
        state_file.write(
            json.dumps(
                {"streamdeck_ui_version": CONFIG_FILE_VERSION, "state": state},
                indent=4,
                separators=(",", ": "),
            )
        )


def open_decks() -> Dict[str, Dict[str, Union[str, Tuple[int, int]]]]:
    """Opens and then returns all known stream deck devices"""
    for deck in DeviceManager.DeviceManager().enumerate():
        deck.open()
        deck.reset()
        deck_id = deck.get_serial_number()
        decks[deck_id] = deck
        deck.set_key_callback(partial(_key_change_callback, deck_id))

    return {
        deck_id: {"type": deck.deck_type(), "layout": deck.key_layout()}
        for deck_id, deck in decks.items()
    }


def ensure_decks_connected() -> None:
    """Reconnects to any decks that lost connection. If they did, re-renders them."""
    for deck_serial, deck in decks.copy().items():
        if not deck.connected():
            for new_deck in DeviceManager.DeviceManager().enumerate():
                try:
                    new_deck.open()
                    new_deck_serial = new_deck.get_serial_number()
                except Exception as error:
                    warn(f"A {error} error occurred when trying to reconnect to {deck_serial}")
                    new_deck_serial = None

                if new_deck_serial == deck_serial:
                    deck.close()
                    new_deck.reset()
                    new_deck.set_key_callback(partial(_key_change_callback, new_deck_serial))
                    decks[new_deck_serial] = new_deck
                    render()


def get_deck(deck_id: str) -> Dict[str, Dict[str, Union[str, Tuple[int, int]]]]:
    return {"type": decks[deck_id].deck_type(), "layout": decks[deck_id].key_layout()}


def _button_state(deck_id: str, page: int, button: int) -> dict:
    buttons = state.setdefault(deck_id, {}).setdefault("buttons", {})
    buttons_state = buttons.setdefault(page, {})  # type: ignore
    return buttons_state.setdefault(button, {})  # type: ignore


def set_button_text(deck_id: str, page: int, button: int, text: str) -> None:
    """Set the text associated with a button"""
    _button_state(deck_id, page, button)["text"] = text
    image_cache.pop(f"{deck_id}.{page}.{button}", None)
    render()
    _save_state()


def get_button_text(deck_id: str, page: int, button: int) -> str:
    """Returns the text set for the specified button"""
    return _button_state(deck_id, page, button).get("text", "")


def set_button_icon(deck_id: str, page: int, button: int, icon: str) -> None:
    """Sets the icon associated with a button"""
    _button_state(deck_id, page, button)["icon"] = icon
    image_cache.pop(f"{deck_id}.{page}.{button}", None)
    render()
    _save_state()


def get_button_icon(deck_id: str, page: int, button: int) -> str:
    """Returns the icon set for a particular button"""
    return _button_state(deck_id, page, button).get("icon", "")


def set_button_change_brightness(deck_id: str, page: int, button: int, amount: int) -> None:
    """Sets the brightness changing associated with a button"""
    _button_state(deck_id, page, button)["brightness_change"] = amount
    render()
    _save_state()


def get_button_change_brightness(deck_id: str, page: int, button: int) -> int:
    """Returns the brightness change set for a particular button"""
    return _button_state(deck_id, page, button).get("brightness_change", 0)


def set_button_command(deck_id: str, page: int, button: int, command: str) -> None:
    """Sets the command associated with the button"""
    _button_state(deck_id, page, button)["command"] = command
    _save_state()


def get_button_command(deck_id: str, page: int, button: int) -> str:
    """Returns the command set for the specified button"""
    return _button_state(deck_id, page, button).get("command", "")


def set_button_switch_page(deck_id: str, page: int, button: int, switch_page: int) -> None:
    """Sets the page switch associated with the button"""
    _button_state(deck_id, page, button)["switch_page"] = switch_page
    _save_state()


def get_button_switch_page(deck_id: str, page: int, button: int) -> int:
    """Returns the page switch set for the specified button. 0 implies no page switch."""
    return _button_state(deck_id, page, button).get("switch_page", 0)


def set_button_keys(deck_id: str, page: int, button: int, keys: str) -> None:
    """Sets the keys associated with the button"""
    _button_state(deck_id, page, button)["keys"] = keys
    _save_state()


def get_button_keys(deck_id: str, page: int, button: int) -> str:
    """Returns the keys set for the specified button"""
    return _button_state(deck_id, page, button).get("keys", "")


def set_button_write(deck_id: str, page: int, button: int, write: str) -> None:
    """Sets the text meant to be written when button is pressed"""
    _button_state(deck_id, page, button)["write"] = write
    _save_state()


def get_button_write(deck_id: str, page: int, button: int) -> str:
    """Returns the text to be produced when the specified button is pressed"""
    return _button_state(deck_id, page, button).get("write", "")


def set_brightness(deck_id: str, brightness: int) -> None:
    """Sets the brightness for every button on the deck"""
    decks[deck_id].set_brightness(brightness)
    state.setdefault(deck_id, {})["brightness"] = brightness
    _save_state()


def get_brightness(deck_id: str) -> int:
    """Gets the brightness that is set for the specified stream deck"""
    return state.get(deck_id, {}).get("brightness", 100)  # type: ignore


def change_brightness(deck_id: str, amount: int = 1) -> None:
    """Change the brightness of the deck by the specified amount"""
    set_brightness(deck_id, max(min(get_brightness(deck_id) + amount, 100), 0))


def get_page(deck_id: str) -> int:
    """Gets the current page shown on the stream deck"""
    return state.get(deck_id, {}).get("page", 0)  # type: ignore


def set_page(deck_id: str, page: int) -> None:
    """Sets the current page shown on the stream deck"""
    state.setdefault(deck_id, {})["page"] = page
    render()
    _save_state()

def spawn_render_thread() -> None:
    thread = threading.Thread(target=render_thread)
    thread.start()
    
# Combine all render threads into 1
# Compute time when to draw next frame, if time passed during iterations, draw it

# Do in chunks
# First get current time
# If time passed:
#  Draw and compute next time

# On new render:
#   update all time to current time, reset frames

def render_thread() -> None:
    t = threading.currentThread()
    i = 0
    while getattr(t, "do_run", True):
        # Store current time for next draw calc
        current_time = round(time.monotonic() * 1000)

        # Draw on each deck
        lock.acquire()
        for deck_id, deck_state in state.items():
            deck = decks.get(deck_id, None)
            if not deck:
                continue

            page = get_page(deck_id)
            for button_id, button_settings in (
                deck_state.get("buttons", {}).get(page, {}).items()  # type: ignore
            ):
                key = f"{deck_id}.{page}.{button_id}"

                if key in image_cache:
                    image, fps, next_time, frame = image_cache[key]
                    # Time to draw next frame passed
                    if fps != 0 and current_time >= next_time:
                        # Update the key images with the next animation frame.
                        next_time = current_time + (1000/fps)
                        
                        image_cache[key] = [image, fps, next_time, (frame+1)%len(image)] # Update image frame
                        deck.set_key_image(button_id, image[frame]) # Draw current frame
                                
                    elif fps == 0 and next_time == 0:
                        image_cache[key] = [image, fps, -1.0, (frame+1)%len(image)] # Update image frame
                        deck.set_key_image(button_id, image[frame]) # Draw current frame
        lock.release()
        time.sleep(1/100)

def render() -> None:
    """renders all decks"""
    lock.acquire()
    for deck_id, deck_state in state.items():
        deck = decks.get(deck_id, None)
        if not deck:
            warn(f"{deck_id} has settings specified but is not seen. Likely unplugged!")
            continue

        page = get_page(deck_id)
        for button_id, button_settings in (
            deck_state.get("buttons", {}).get(page, {}).items()  # type: ignore
        ):
            key = f"{deck_id}.{page}.{button_id}"
            # Not updating all causes sync issues in the gifs themselves
            if key not in image_cache:
                image, fps = _render_key_image(deck, **button_settings)
                image_cache[key] = [image, fps, 0.0, 0] # Start on frame 0
            else:
                image, fps, *rest = image_cache[key]
                image_cache[key] = [image, fps, 0.0, 0]
    lock.release()

def _render_key_image(deck, icon: str = "", text: str = "", font: str = DEFAULT_FONT, **kwargs):
    """Renders an individual key image"""
    icon_frames = list()

    if icon:
        icons = Image.open(icon)
        frames = ImageSequence.Iterator(icons)
        
        if 'duration' in icons.info.keys():
            fps = 1000 / icons.info['duration'] # https://stackoverflow.com/questions/53364769/get-frames-per-second-of-a-gif-in-python
        else:
            fps = 0
    else:
        fps = 0
        frames = ImageSequence.Iterator(Image.new("RGBA", (300, 300)))
    

    for frame in frames:
        image = ImageHelpers.PILHelper.create_image(deck)
        draw = ImageDraw.Draw(image)

        rgba_icon = frame.convert("RGBA")

        alpha = rgba_icon.split()[-1]
        bg = Image.new('RGBA', rgba_icon.size, (0,0,0))
        bg.paste(rgba_icon, mask=alpha)
        rgba_icon = bg

        icon_width, icon_height = image.width, image.height
        # Shift image up when text present
        if text:
            icon_height -= 20

        rgba_icon.thumbnail((icon_width, icon_height), Image.LANCZOS)
        icon_pos = ((image.width - rgba_icon.width) // 2, 0)
        image.paste(rgba_icon, icon_pos, rgba_icon)

        if text:
            true_font = ImageFont.truetype(os.path.join(FONTS_PATH, font), 14)
            label_w, label_h = draw.textsize(text, font=true_font)
            if icon:
                # This here shifts text down when image present
                label_pos = ((image.width - label_w) // 2, image.height - 20)
            else:
                label_pos = ((image.width - label_w) // 2, (image.height // 2) - 7)
            draw.text(label_pos, text=text, font=true_font, fill="white")

        # Store the rendered animation frame in the device's native image
        # format for later use, so we don't need to keep converting it.
        icon_frames.append(PILHelper.to_native_format(deck, image))
    
    # Return an infinite cycle generator that returns the next animation frame
    # each time it is called.
    return icon_frames, fps

if os.path.isfile(STATE_FILE):
    _open_config(STATE_FILE)
