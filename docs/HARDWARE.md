# KAT Walk C2+ ("plusE") - hardware model

How the unit is built and behaves. The protocol decoding in `PLUSE-PROTOCOL.md` fits this model.

## Parts

| Part | Power | Link | Role | USB device |
|------|-------|------|------|------------|
| **Base** (stationary platform you stand on) | mains, always on | **USB** (the device the PC sees) | wireless receiver for everything else; has a vibration motor (haptics out) | the hub `2109:2822` exposing 3 HID interfaces |
| **Rotating base** (waist ring, turns as you turn in place) | battery, sleeps | wireless to base | body heading / facing direction | carried inside the `c4f4:3f12` receiver stream |
| **Shoes** (one per foot) | battery, sleeps | wireless to base | optical sensor on each sole, 2D motion (fwd/back/strafe + speed) | carried inside the `c4f4:3f12` receiver stream |
| **Seat addon** (optional) | battery | wireless to base | fold-down seat; sit instead of run | `c4f4:bf12` "position" (Vehicle Hub) |
| **Armband** (optional addon) | battery, sleeps | wireless to base | heart rate only, not used for locomotion | `c4f4:bf13` "armband" |

The `c4f4:3f12` "receiver" is the aggregator: it carries the body heading and both feet in one
stream, and it is the device you read for locomotion. `bf12` is the seat / Vehicle Hub, `bf13` is
the armband.

## Locomotion model

- **Body heading** (from the receiver) is which direction the player faces.
- **Shoes** (optical) give whether the player is moving forward, sideways, or backward, and how
  fast: the optical slip of the grounded foot on the deck is the movement velocity.
- Combined: direction is the body heading plus the foot-slip angle, speed is the foot-slip
  magnitude. See `PLUSE-PROTOCOL.md` for the frame layout and `katwalk/locomotion.py` for the model.

## Sleep behavior (the #1 capture constraint)

Everything except the base is battery-powered and sleeps when idle. So:

- An idle rig streams little or nothing. The shoes and rotating base must be awake and moving to
  produce data.
- They wake on motion and sleep after a timeout, so captures of an idle rig come out empty or
  intermittent.
- This wireless sleep behavior is also a plausible contributor to the idle USB `-71` flapping in
  `USB-NOISE-FIX.md`.

## Base outputs

- **LED:** a single blue LED, brightness only (no RGB, no color). The command to set its
  brightness has not been worked out yet.
- **Vibration motor:** in the base, driven by the `0xA1` vendor OUTPUT command (intensity is a
  big-endian 16-bit value); `0xA0` stops it and is also the sleep command. See `PLUSE-PROTOCOL.md`.

## Priorities

1. Body heading + shoes (movement): the locomotion core.
2. Vibration motor (haptics out): nice to have.
3. Seat state: optional.
4. Armband (HR): not gameplay-relevant.
