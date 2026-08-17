# imla

Push-to-talk dictation for Windows. Hit the hotkey, talk, hit it again. What you
said gets transcribed and pasted into whatever window has focus.

I wrote it to dictate prompts into Claude Code, but it works anywhere Ctrl+V
does.

Whisper runs locally on the GPU through faster-whisper, so there's no API key,
no account, and no audio leaving the machine.

## Setup

Python 3.13, then:

```
pip install sounddevice faster-whisper
```

faster-whisper brings CTranslate2, and the CUDA libraries (cuBLAS, cuDNN) arrive
as pip packages too, about 2 GB of them. No system CUDA toolkit required. The
large-v3 weights are another 3 GB and download on first use into
`%USERPROFILE%\.cache\huggingface`.

Check it works before wiring up the hotkey:

```
python imla.py --test 5
```

Records five seconds, prints the transcript, pastes nothing.

## Running

`imla.cmd` leaves a console window up so you can watch transcripts go by.
Once you trust it, `imla-background.vbs` runs windowless and writes to
`imla.log`. To start it at login, put a shortcut to the `.vbs` in:

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
```

## Using it

Focus a window, press Ctrl+Shift+Space, talk, press it again.

The beeps:

- rising pair, plus a toast: model is loaded, hotkey is live
- one rising: recording
- falling: stopped, transcribing
- short high: pasted
- two low: something broke, check the log

large-v3 needs about 12 seconds to get onto the GPU and the hotkey does nothing
until it's there. Running windowless that ready beep is the only signal you get,
so wait for it.

Recording stops itself at `max_seconds` (300) so a session you walked away from
doesn't run all afternoon.

On my machine (3080 Ti, Ryzen 7 5800) a 10-second clip comes back in about a
second, and the model holds ~2.2 GB of VRAM the whole time it's loaded.

## Config

`python imla.py --setup` covers the ones you'll actually change. The rest
live in `%APPDATA%\imla\config.json`:

| Key | Default | Notes |
|---|---|---|
| `backend` | `local` | `local` is on-device, `api` posts to an OpenAI-compatible endpoint |
| `local_model` | `large-v3` | `medium`, `small`, `tiny` are faster and worse |
| `local_device` | `auto` | drops to CPU if CUDA isn't there |
| `local_compute_type` | `int8_float16` | `float16` for fidelity, `int8` on CPU |
| `local_beam_size` | `5` | lower is a little faster, a little worse |
| `language` | `""` | ISO code, `tr` or `en`; blank auto-detects |
| `prompt` | `""` | bias the model toward names and jargon you use |
| `hotkey` | `ctrl+shift+space` | e.g. `ctrl+alt+d`, `win+f9` |
| `input_device` | `null` | number from `--devices`, null follows Windows |
| `max_seconds` | `300` | auto-stop |
| `paste` | `true` | false copies to the clipboard without sending Ctrl+V |
| `sounds` | `true` | the beeps |
| `cleanup` | `false` | filler-word removal, needs the `api` backend |

Two of these are worth setting properly.

`language`: large-v3 is multilingual and its Turkish is good, but auto-detect
guesses wrong often enough on short clips to be annoying. Pinning
`"language": "tr"` kills that off.

`prompt`: Whisper conditions on it, so listing the project names and libraries
you say constantly does visibly improve how it spells them.

## Flags

```
python imla.py               # run with the configured hotkey
python imla.py --setup       # interactive config
python imla.py --devices     # list microphones
python imla.py --test 5      # record 5s, print it, don't paste
python imla.py --no-paste    # clipboard only
python imla.py --model medium
python imla.py --hotkey ctrl+shift+d
```

## How it works

`RegisterHotKey` claims the shortcut system-wide and a `GetMessageW` loop waits
on `WM_HOTKEY`. PortAudio captures 16 kHz mono int16 into an in-memory WAV, no
temp files. faster-whisper transcribes it. The text goes onto the clipboard
through the Win32 API, Ctrl+V goes out via `SendInput`, and whatever was on your
clipboard before gets put back a moment later.
