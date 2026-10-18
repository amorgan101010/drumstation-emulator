"""Native UI regression check for the desktop panel (runs under Xvfb, no engine needed).

Verifies that knob tick rings stay inside their cards and never overlap, that
captions do not collide with rings, other captions or controls, that each kit
block's rows share their edges, that button legends keep clear of their
button's edges and of each other, that every required control (27 HIT
buttons, keypad, MODE, demo, WRITE) is present, and that the pre-rendered
artwork in tools/assets was built for the current geometry.
"""
import sys
import tkinter as tk
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import gui  # noqa: E402

TICK = gui.TICK
PAD_X = 6  # least space between a button's legend and its left or right edge


def circle_hits_rect(cx, cy, r, rect):
    x0, y0, x1, y1 = rect
    nx, ny = min(max(cx, x0), x1), min(max(cy, y0), y1)
    return (cx - nx) ** 2 + (cy - ny) ** 2 < r * r


def overlap(a, b, pad=0):
    return a[0] < b[2] - pad and b[0] < a[2] - pad and a[1] < b[3] - pad and b[1] < a[3] - pad


def check(panel: gui.DrumStationPanel) -> list[str]:
    c, problems = panel.canvas, []
    cards = panel.cards
    want = [f"{kit} {title}" for kit, _, rows in gui.KIT_BLOCKS for cells in rows for title, _ in cells]
    if [title for title, _ in cards] != want or len(set(want)) != 27:
        problems.append(f"expected 27 distinct kit cards, drew {[title for title, _ in cards]}")
    for kit, _, _ in gui.KIT_BLOCKS:
        rows = {}
        for title, (x0, y0, x1, _) in cards:
            if title.startswith(kit):
                rows.setdefault(y0, []).append((x0, x1))
        edges = {(min(s[0] for s in spans), max(s[1] for s in spans)) for spans in rows.values()}
        if len(edges) != 1:
            problems.append(f"{kit} card rows do not share left/right edges: {sorted(edges)}")
    for kit, _, rows in gui.KIT_BLOCKS:
        widths = {}
        for cells in rows:
            for title, w in cells:
                widths.setdefault(len(gui.CARD_CONTROLS[title]), set()).add(w)
        for count, found in widths.items():
            if len(found) != 1:
                problems.append(f"{kit} cards with {count} knobs have different widths: {sorted(found)}")
    for i, (ta, a) in enumerate(cards):
        if a[0] < 47 or a[2] > gui.PANEL_W - 47 or a[3] > 889:
            problems.append(f"{ta} card leaves the card area")
        for tb, b in cards[i + 1:]:
            if overlap(a, b):
                problems.append(f"cards {ta} and {tb} overlap")
    rings = [(k, k.radius + TICK) for k in panel.knobs]
    for k, r in rings:
        home = [t for t, rect in cards if rect[0] <= k.x <= rect[2] and rect[1] <= k.y <= rect[3]]
        if home:
            x0, y0, x1, y1 = dict(cards)[home[0]]
            if k.x - r < x0 + 2 or k.x + r > x1 - 2 or k.y - r < y0 + gui.CARD_TITLE_H or k.y + r > y1 - 2:
                problems.append(f"{home[0]} {k.label}: tick ring leaves its card")
        elif not (k.y - r >= 29 and k.y + r <= 136):
            problems.append(f"header knob {k.label} leaves the header")
    for i, (a, ra) in enumerate(rings):
        for b, rb in rings[i + 1:]:
            if (a.x - b.x) ** 2 + (a.y - b.y) ** 2 < (ra + rb) ** 2:
                problems.append(f"knobs {a.label}@{a.x},{a.y} and {b.label}@{b.x},{b.y} overlap")
    buttons = [rect for rect, _ in panel.button_hits]
    texts, legends = [], {}
    for item in c.find_all():
        if c.type(item) == "text" and c.itemcget(item, "text").strip():
            box = c.bbox(item)
            label = c.itemcget(item, "text").replace("\n", " ")
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            home = [b for b in buttons if b[0] <= cx <= b[2] and b[1] <= cy <= b[3]]
            inside = any(b[0] - 1 <= box[0] and box[2] <= b[2] + 1 and b[1] - 1 <= box[1] and box[3] <= b[3] + 1
                         for b in buttons)
            texts.append((label, box, inside))
            if home:
                legends.setdefault(home[0], []).append((label, box))
    for (x0, y0, x1, y1), group in legends.items():
        for i, (label, box) in enumerate(group):
            if box[0] < x0 + PAD_X or box[2] > x1 - PAD_X or box[1] < y0 or box[3] > y1:
                problems.append(f"legend '{label}' crowds its button {(x0, y0, x1, y1)}: {box}")
            for other, b in group[i + 1:]:
                if overlap(box, b):
                    problems.append(f"legends '{label}' and '{other}' collide on one button")
    for label, box, inside in texts:
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        for title, (x0, y0, x1, y1) in cards:
            if x0 <= cx <= x1 and y0 <= cy <= y1 and not (x0 + 2 <= box[0] and box[2] <= x1 - 2 and box[3] <= y1 - 1):
                problems.append(f"text '{label}' leaves the {title} card")
        for k, r in rings:
            if circle_hits_rect(k.x, k.y, r, box):
                problems.append(f"text '{label}' touches the {k.label} knob ring at {k.x},{k.y}")
        if not inside:
            for b in buttons:
                if overlap(box, b, pad=1):
                    problems.append(f"text '{label}' overlaps a control at {b}")
    loose = [(l, b) for l, b, inside in texts if not inside]
    for i, (la, a) in enumerate(loose):
        for lb, b in loose[i + 1:]:
            if overlap(a, b, pad=1):
                problems.append(f"text '{la}' overlaps text '{lb}'")
    for i, a in enumerate(buttons):
        for b in buttons[i + 1:]:
            if overlap(a, b):
                problems.append(f"controls {a} and {b} overlap")
        for k, r in rings:
            if circle_hits_rect(k.x, k.y, r, a):
                problems.append(f"control {a} overlaps the {k.label} knob ring")
    labels = [label for label, _, _ in texts]
    for name, want in (("HIT", 27), ("NOTE-OFF", 27), ("VEL", 75), ("MODE", 1), ("FACTORY DEMO", 1), ("WRITE", 1)):
        if labels.count(name) != want:
            problems.append(f"expected {want} '{name}' labels, found {labels.count(name)}")
    for key, caption in gui.KEYS:
        if key not in labels:
            problems.append(f"keypad key {key} is missing")
    return problems


def check_artwork(panel: gui.DrumStationPanel) -> list[str]:
    problems = []
    if panel.skin.meta["fingerprint"] != gui.layout_fingerprint():
        problems.append("tools/assets is stale for this layout: run python3 tools/render_panel.py")
    want = {f"knob{r}:{i}" for r in (gui.MAIN_R, gui.MINI_R, gui.HEADER_R) for i in range(gui.KNOB_FRAMES)}
    want |= {f"{kind}:{style}:{down}" for kind in gui.BUTTON_SIZES for style in gui.BUTTON_STYLES for down in (0, 1)}
    want |= {f"segment:{bit}" for bit in gui.SEGMENT_BITS} | {"led:amber", "led:green", "led:red"}
    if want - set(panel.skin.sprites):
        problems.append(f"artwork is missing sprites: {sorted(want - set(panel.skin.sprites))[:8]}")
    plate = panel.skin.plate
    if (plate.width(), plate.height()) != (gui.PANEL_W, gui.PANEL_H):
        problems.append(f"plate is {plate.width()}x{plate.height()}, not the panel's size")
    for plate, size in ((panel.skin.sequencer_plate, gui.SEQUENCER_SIZE),
                        (panel.skin.sequencer_full_plate, gui.SEQUENCER_FULL_SIZE),
                        (panel.skin.sequencer_all_plate, gui.SEQUENCER_ALL_SIZE)):
        if (plate.width(), plate.height()) != size:
            problems.append(f"sequencer plate is {plate.width()}x{plate.height()}, expected {size}")
    return problems


def check_lock_gestures(panel: gui.DrumStationPanel) -> list[str]:
    """Exercise held-step wheel and modifier-drag edits through native handlers."""
    failures = []
    track = "808:BASS DRUM"
    panel.open_sequencer(track)
    panel.root.update()
    window = panel.sequence_window
    bounds, _track, _step = window.step_buttons[0]
    x, y = (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2
    knob = next(k for k in panel.knobs if k.track == track and k.key.startswith("cc:"))
    base = panel.values.get(knob.key, 64)

    window.press(SimpleNamespace(x=x, y=y))
    panel.mouse_wheel(SimpleNamespace(
        x_root=panel.canvas.winfo_rootx() + knob.x,
        y_root=panel.canvas.winfo_rooty() + knob.y,
        delta=120, num=None, state=0,
    ))
    window.release(SimpleNamespace(x=x, y=y))
    if not panel.sequencer.trig_on(track, 0):
        failures.append("held trig + knob wheel toggled the trig back off")
    lock_value = panel.sequencer.locks_for(track, 0).get(knob.key)
    if lock_value is None or panel.values.get(knob.key, 64) != base:
        failures.append("held trig + knob wheel did not save a lock separately from the base value")
    if knob.key in panel.lock_display:
        failures.append("held trig release did not restore the base-value display")

    panel.press(SimpleNamespace(x=knob.x, y=knob.y, state=0x0001))
    panel.drag_knob(SimpleNamespace(y=knob.y - 20))
    panel.flush_drag()
    panel.release_drag(SimpleNamespace())
    edited = panel.sequencer.locks_for(track, 0).get(knob.key)
    if lock_value is not None and (edited is None or edited <= lock_value):
        failures.append("Shift-drag did not increase the selected trig's existing lock")
    if panel.values.get(knob.key, 64) != base:
        failures.append("Shift-drag changed the base value while editing a lock")
    if knob.key in panel.lock_display:
        failures.append("lock display did not return to the base-value display after release")
    return failures


def check_sequencer_layout(window: gui.SequencerWindow) -> list[str]:
    """Check the detached window's band spacing and text/control clearance."""
    problems = []
    height = (gui.SEQUENCER_ALL_SIZE[1] if window.all_tracks else
              gui.SEQUENCER_FULL_SIZE[1] if window.full_pattern else gui.SEQUENCER_SIZE[1])
    bands = ((39, 19, 1401, 71), (39, 82, 1401, 139),
             (39, 150, 1401, 231), (39, 242, 1401, height - 99),
             (39, height - 87, 1401, height - 16))
    buttons = [bounds for bounds, _action in window.buttons]
    expected = (len(gui.TRACK_IDS) if window.all_tracks else 1) * (64 if window.full_pattern else 16)
    if len(window.step_buttons) != expected:
        problems.append(f"expected {expected} visible steps, found {len(window.step_buttons)}")
    for index, a in enumerate(buttons):
        if not any(b[0] <= a[0] and a[2] <= b[2] and b[1] <= a[1] and a[3] <= b[3]
                   for b in bands):
            problems.append(f"sequencer button {a} leaves its control band")
        for b in buttons[index + 1:]:
            if overlap(a, b):
                problems.append(f"sequencer buttons {a} and {b} overlap")

    canvas = window.canvas
    loose = []
    for item in canvas.find_all():
        if canvas.type(item) != "text":
            continue
        label = canvas.itemcget(item, "text")
        box = canvas.bbox(item)
        if not label.strip() or box is None:
            continue
        if not any(b[0] <= box[0] and box[2] <= b[2] and b[1] <= box[1] and box[3] <= b[3]
                   for b in bands):
            problems.append(f"sequencer text '{label}' leaves its band")
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        home = next((b for b in buttons if b[0] <= cx <= b[2] and b[1] <= cy <= b[3]), None)
        if home is not None:
            if not (home[0] + 4 <= box[0] and box[2] <= home[2] - 4 and
                    home[1] + 2 <= box[1] and box[3] <= home[3] - 2):
                problems.append(f"sequencer button label '{label}' crowds {home}")
        else:
            loose.append((label, box))
            for b in buttons:
                if overlap(box, b, pad=1):
                    problems.append(f"sequencer text '{label}' overlaps button {b}")
    for index, (label, box) in enumerate(loose):
        for other, other_box in loose[index + 1:]:
            if overlap(box, other_box, pad=1):
                problems.append(f"sequencer text '{label}' overlaps '{other}'")
    return problems


def main() -> int:
    root = tk.Tk()
    root.geometry(f"{gui.PANEL_W}x{gui.PANEL_H}")
    # Geometry only: the constructor schedules start_engine, so stub it on
    # the class before construction or a real engine opens the audio device.
    gui.DrumStationPanel.start_engine = lambda self: None
    panel = gui.DrumStationPanel(root, settings=None, sequencer_path=None)
    panel.draw()
    root.update()
    failures = check_artwork(panel) + check(panel)
    # Widest captions: individual outputs, 99 ms front cut, full distortion.
    for _, pan in {a for pair in gui.PAN_ADDRESS.values() for a in enumerate(pair)}:
        panel.values.update({f"pan:{pan:03x}": 14, f"dist:{pan:03x}": 15, f"cut:{pan - 1:03x}": 99})
    panel.draw()
    root.update()
    failures += [f"[widest captions] {p}" for p in check(panel)]
    try:
        failures += [f"[lock gestures] {p}" for p in check_lock_gestures(panel)]
        failures += [f"[sequencer layout] {p}" for p in check_sequencer_layout(panel.sequence_window)]
        window = panel.sequence_window
        for which in ("steps", "tracks"):
            window.toggle_view(which)
            root.update()
            failures += [f"[{which} view] {p}" for p in check_sequencer_layout(window)]
        target = (gui.TRACK_IDS[-1], 63)
        bounds = window.cell_bounds[target]
        x, y = (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2
        was_on = panel.sequencer.trig_on(*target)
        window.press(SimpleNamespace(x=x, y=y))
        window.release(SimpleNamespace(x=x, y=y))
        if panel.sequencer.trig_on(*target) == was_on or panel.selected_track != target[0]:
            failures.append("[combined view] click did not select and toggle track 27 step 64")
        window.toggle_view("steps")
        root.update()
        failures += [f"[all tracks view] {p}" for p in check_sequencer_layout(window)]
        window.toggle_view("tracks")
        root.update()
        if window.all_tracks or window.full_pattern:
            failures.append("view toggles did not restore the default view")
    except Exception as error:
        failures.append(f"[sequencer window] check failed: {error}")
    panel.close()
    for p in failures:
        print(p)
    print("layout OK" if not failures else f"{len(failures)} layout problem(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
