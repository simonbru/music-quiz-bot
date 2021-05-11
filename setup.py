#!/usr/bin/env python
from setuptools import setup

install_requires = [
    "httpx",
    "discord.py",
]

setup(
    name="music_quiz_bot",
    version="1.0",
    packages=["music_quiz_bot"],
    package_dir={"": "src"},
    description="Music quiz Discord bot using data from Spotify.",
    long_description="",
    author="Sylvain Fankhauser, Simon Brulhart",
    author_email="sephi@fhtagn.top",
    url="https://github.com/simonbru/music-quiz-bot",
    install_requires=install_requires,
    license="mit",
    include_package_data=False,
    python_requires=">=3.7",
    entry_points={"console_scripts": "music-quiz-bot = music_quiz_bot.discord_bot:main"},
    classifiers=["License :: OSI Approved :: MIT License"],
)
