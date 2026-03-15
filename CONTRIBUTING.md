# Contributing

Thank you for considering contributing to Spotify Widget!

## How to contribute

### Reporting bugs

1. Check if the issue already exists in [Issues](../../issues)
2. Open a new issue using the **Bug Report** template
3. Include your OS, Python version, and full error output

### Suggesting features

1. Open a new issue using the **Feature Request** template
2. Describe the use case clearly

### Submitting code

1. Fork this repository
2. Create a branch: `git checkout -b feature/your-feature-name`
3. Make your changes
4. Test that the widget runs correctly: `python3 spotify-widget.py`
5. Commit with a clear message: `git commit -m "Add: brief description"`
6. Push and open a Pull Request

## Code style

- Follow [PEP 8](https://peps.python.org/pep-0008/)
- Keep functions small and focused
- Add comments for non-obvious logic
- Do not commit credentials or personal tokens

## Development setup

```bash
git clone https://github.com/GustavoGamarra95/spotify-widget.git
cd spotify-widget

# Install dependencies
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad playerctl

pip install spotipy

# Run
python3 spotify-widget.py
```

## Questions?

Open an issue or start a Discussion on GitHub.
