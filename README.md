# CCTV Lite

**What it is.** CCTV Lite is a live multi-camera viewer for a local NVR on **Omarchy** (Arch Linux + Hyprland). Point it at a Hikvision-style recorder over RTSP and watch cameras in a grid (sub or main stream, named layouts). It is a lightweight GTK viewer, not a full NVR.

GTK4 + libadwaita + GStreamer.

`omarchy install cctv-lite` installs the viewer **and** enables the bar plugin ([cytracon/omarchy-cytracon-bar-cctv-lite](https://github.com/cytracon/omarchy-cytracon-bar-cctv-lite)).

## Privacy

Camera host, password, and layout live only in **`~/.config/cctv-lite/config.yaml`** (mode 600). That file is never part of this repository. `data/config.yaml.example` is an empty template.

## Install on Omarchy

```bash
omarchy install cctv-lite
```

or from a checkout:

```bash
./install-local.sh
```

Existing `~/.config/cctv-lite/config.yaml` is kept. The installer does not write credentials.

## CLI

```bash
cctv-lite --version
cctv-lite --status          # JSON for the bar plugin; no secrets
cctv-lite --sync-names      # optional ISAPI name refresh
```

## Plugin

```bash
omarchy plugin add https://github.com/cytracon/omarchy-cytracon-bar-cctv-lite.git --enable
```

## License

MIT.
