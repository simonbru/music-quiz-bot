# Music quiz Discord bot

This quiz uses data from Spotify to play a sample of a track, and then
letting players guess the track.

## Installation

To install the bot, fetch its source code (`git clone
https://github.com/simonbru/music-quiz-bot`) and install it, preferrably in a virtual
environment (`python3 -m venv music_quiz_bot_venv`), using `python3 -m pip install
/path/to/music_quiz_bot_dir`.

You’ll also need to set the following environment variables:

* `DISCORD_TOKEN`: the token of the Discord bot. See
  https://discord.com/developers/applications for more information
  
Once this is done, eg. by exporting those environment variables, run the
`music-quiz-bot` command.
