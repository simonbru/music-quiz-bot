import asyncio
import dataclasses
import datetime
import difflib
import enum
import io
import json
import logging
import os
import random
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List

import discord

from music_quiz_bot.table import Heading, Justify, Table
from music_quiz_bot.quiz import MusicQuizSession

NB_SAMPLES = 12
GUESS_TIME_SECONDS = 30
MAX_COMBO = 2
STATS_DIR = os.environ.get(
    "STATS_DIR", os.path.join(os.path.abspath(os.path.dirname(__file__)), "stats")
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class GameStatus(enum.Enum):
    IDLE = enum.auto()
    WAITING_FOR_GUESSES = enum.auto()
    LOADING = enum.auto()


class CommandType(enum.Enum):
    START = "start"
    HELP = "help"
    STATS = "stats"


@dataclass(frozen=True)
class Command:
    type: CommandType
    args: List[str]


@dataclass(frozen=True)
class Combo:
    player: str
    combo: int


@dataclass(frozen=True)
class FuzzyResult:
    match: str
    score: float


@dataclass(frozen=True)
class Stat:
    player_id: str
    player_name: str
    nb_guesses: int
    nb_samples_played: int
    nb_correct_guesses: int
    nb_skips: int
    nb_aces: int
    max_streak: int
    reaction_time: float
    precision: float


@dataclass(frozen=True)
class PlayerStat:
    player_id: str
    player_name: str
    nb_guesses: int
    nb_samples_played: int
    nb_correct_guesses: int
    nb_skips: int
    nb_aces: int
    max_streak: int
    reaction_time: float
    precision: float
    nb_games: int = 1

    def __add__(self, other):
        return PlayerStat(
            player_id=other.player_id,
            player_name=other.player_name,
            nb_guesses=self.nb_guesses + other.nb_guesses,
            nb_samples_played=self.nb_samples_played + other.nb_samples_played,
            nb_correct_guesses=self.nb_correct_guesses + other.nb_correct_guesses,
            nb_skips=self.nb_skips + other.nb_skips,
            nb_aces=self.nb_aces + other.nb_aces,
            max_streak=max(self.max_streak, other.max_streak),
            reaction_time=(
                self.reaction_time * self.nb_samples_played
                + other.reaction_time * other.nb_samples_played
            )
            / (self.nb_samples_played + other.nb_samples_played)
            if self.nb_samples_played + other.nb_samples_played > 0
            else 0,
            precision=(
                self.precision * self.nb_correct_guesses
                + other.precision * other.nb_correct_guesses
            )
            / (self.nb_correct_guesses + other.nb_correct_guesses)
            if self.nb_correct_guesses + other.nb_correct_guesses > 0
            else 0,
            nb_games=self.nb_games + 1,
        )

    @property
    def avg_guesses_per_game(self):
        return self.nb_guesses / self.nb_games

    @property
    def avg_correct_guesses_per_game(self):
        return self.nb_correct_guesses / self.nb_games

    @property
    def correct_guesses_ratio(self):
        return self.nb_correct_guesses / self.nb_guesses * 100


@dataclasses.dataclass()
class GameStats:
    stats: Dict[int, Stat] = dataclasses.field(default_factory=dict)
    started_at: datetime.datetime = dataclasses.field(
        default_factory=lambda: datetime.datetime.utcnow()
    )

    def get_stat(self, player_id, player_name):
        try:
            return self.stats[player_id]
        except KeyError:
            return Stat(
                player_id=player_id,
                player_name=player_name,
                nb_guesses=0,
                nb_samples_played=0,
                nb_correct_guesses=0,
                reaction_time=0,
                nb_skips=0,
                nb_aces=0,
                max_streak=0,
                precision=0,
            )

    def skip(self, player_id, player_name):
        stat = self.get_stat(player_id, player_name)
        self.stats[player_id] = dataclasses.replace(stat, nb_skips=stat.nb_skips + 1)

    def guess(
        self,
        player_id,
        player_name,
        is_correct,
        is_ace,
        reaction_time,
        streak,
        precision,
    ):
        stat = self.get_stat(player_id, player_name)
        self.stats[player_id] = dataclasses.replace(
            stat,
            nb_guesses=stat.nb_guesses + 1,
            nb_samples_played=stat.nb_samples_played + (0 if reaction_time is None else 1),
            nb_correct_guesses=stat.nb_correct_guesses + (1 if is_correct else 0),
            reaction_time=(stat.reaction_time * stat.nb_samples_played + reaction_time)
            / (stat.nb_samples_played + 1)
            if reaction_time is not None
            else stat.reaction_time,
            nb_aces=stat.nb_aces + (1 if is_ace else 0),
            max_streak=max(stat.max_streak, streak),
            precision=(stat.precision * stat.nb_correct_guesses + precision)
            / (stat.nb_correct_guesses + 1)
            if precision is not None
            else stat.precision,
        )

    @staticmethod
    def load(file_path) -> List["GameStats"]:
        with open(file_path, "r") as f:
            stats_list = json.loads(f.read())

        stats = [
            GameStats(
                started_at=datetime.datetime.fromtimestamp(
                    stat["started_at"], datetime.timezone.utc
                ),
                stats={
                    int(player_id): Stat(**obj)
                    for player_id, obj in stat["stats"].items()
                },
            )
            for stat in stats_list
        ]

        return stats

    def asdict(self):
        return {
            "started_at": self.started_at.timestamp(),
            "stats": {
                player_id: dataclasses.asdict(stat)
                for player_id, stat in self.stats.items()
            },
        }


def get_stats_file_path(game_id: int) -> str:
    return os.path.join(STATS_DIR, f"{game_id}.json")


def _fuzzy_compare_str(str1, str2):
    str1, str2 = str1.lower(), str2.lower()

    str1_parts = str1.split(":") + [str1]
    max_ratio = max(
        difflib.SequenceMatcher(lambda x: x in " \t", str1_part, str2).ratio()
        for str1_part in str1_parts
    )

    return max_ratio


def fuzzy_compare(solutions, guess):
    """
    Compare a list of solutions and a guess, and return the highest scoring one
    as a `FuzzyResult`, or `None` if there???s no match.
    """
    results = [
        FuzzyResult(match=solution, score=_fuzzy_compare_str(solution, guess))
        for solution in solutions
    ]

    return max(results, key=lambda item: item.score, default=None)


class Round:
    def __init__(self, sample):
        self.sample = sample
        self.started_at = None
        self.guessers = set()
        self.skip_votes = set()

    def start(self):
        self.started_at = time.monotonic()
        return asyncio.create_task(asyncio.sleep(GUESS_TIME_SECONDS))

    @property
    def elapsed_time(self):
        return time.monotonic() - self.started_at if self.started_at else 0

    def guess(self, player_id, guess):
        fuzzy_result = fuzzy_compare(
            # TODO: use relevant sample fields
            {self.sample.title}, guess,
        )
        self.guessers.add(player_id)

        return fuzzy_result


class Game:
    def __init__(self):
        self.scores = defaultdict(int)
        self.stats = GameStats()
        self.quiz_session = MusicQuizSession()
        self.status = GameStatus.IDLE
        self.guess_timer = None
        self.samples_queue = asyncio.Queue()
        self.signal_subscribers = defaultdict(list)
        self.current_round = None
        self.current_combo = None

    @property
    def nb_players(self):
        return len(self.scores)

    async def handle_guess(self, player_id, player_name, guess, **kwargs):
        if player_name not in self.scores:
            self.scores[player_name] = 0

        first_guess = player_id not in self.current_round.guessers
        fuzzy_result = self.current_round.guess(player_id, guess)

        if fuzzy_result and fuzzy_result.score >= 0.8:
            if self.current_combo and self.current_combo.player == player_name:
                self.current_combo = dataclasses.replace(
                    self.current_combo, combo=self.current_combo.combo + 1,
                )
                streak = self.current_combo.combo
            else:
                self.current_combo = Combo(player=player_name, combo=1)
                streak = 1

            self.stats.guess(
                player_id=player_id,
                player_name=player_name,
                is_correct=True,
                reaction_time=self.current_round.elapsed_time if first_guess else None,
                is_ace=first_guess,
                streak=streak,
                precision=fuzzy_result.score,
            )

            self.scores[player_name] += min(self.current_combo.combo, MAX_COMBO)

            self.status = GameStatus.LOADING
            await self.emit_signal(
                "correct_guess",
                player=player_name,
                track_title=fuzzy_result.match,
                **kwargs,
            )
            self.guess_timer.cancel()
        else:
            self.stats.guess(
                player_id=player_id,
                player_name=player_name,
                is_correct=False,
                reaction_time=self.current_round.elapsed_time if first_guess else None,
                is_ace=False,
                streak=0,
                precision=None,
            )
            if self.current_combo and self.current_combo.player == player_name:
                self.current_combo = None
            await self.emit_signal(
                "incorrect_guess", player=player_name, guess=guess, **kwargs
            )

    async def game_loop(self):
        self.status = GameStatus.LOADING
        self.populate_queue_task = asyncio.create_task(self.populate_queue())
        guess_loop_task = asyncio.create_task(self.guess_loop())

        await self.populate_queue_task
        await guess_loop_task

    async def populate_queue(self):
        for i in range(NB_SAMPLES):
            logging.debug("Fetching sample...")
            sample = await self.quiz_session.get_random_sample(require_solution=True)
            logging.debug("Got sample, putting it in the queue")
            await self.samples_queue.put(sample)

    async def guess_loop(self):
        logging.info("Starting guess loop")
        sample_number = 1
        while not self.populate_queue_task.done() or self.samples_queue.qsize() > 0:
            logging.debug("Getting sample from queue")
            sample = await self.samples_queue.get()
            logging.debug("Got sample from queue")
            logging.debug("Track title: %s", sample.track_title)

            self.current_round = Round(sample)
            await self.emit_signal("new_sample", sample_number=sample_number)
            self.guess_timer = self.current_round.start()
            self.status = GameStatus.WAITING_FOR_GUESSES

            try:
                await self.guess_timer
            except asyncio.CancelledError:
                self.status = GameStatus.IDLE
            else:
                self.current_combo = None
                self.status = GameStatus.IDLE
                await self.emit_signal("sample_timeout")

            # Sleep a bit after the solution was shown to let people cool down
            await asyncio.sleep(3)

            sample_number += 1

        await self.emit_signal("game_finished")

    async def skip(self):
        await self.emit_signal("sample_skipped")
        self.guess_timer.cancel()

    async def emit_signal(self, signal_name, *args, **kwargs):
        subscribers_to_notify = self.signal_subscribers[signal_name]
        if len(subscribers_to_notify) > 0:
            asyncio.gather(
                *(subscriber(*args, **kwargs) for subscriber in subscribers_to_notify)
            )

    def subscribe_to_signal(self, signal_name, callback):
        self.signal_subscribers[signal_name].append(callback)

    async def vote_skip(self, player_id, player_name):
        logger.debug("Player voted to skip")
        if self.status != GameStatus.WAITING_FOR_GUESSES:
            return

        self.current_round.skip_votes.add(player_name)
        self.stats.skip(player_id, player_name)

        if len(self.current_round.skip_votes) >= len(self.scores) // 2:
            # Reset combo if the user holding the combo has voted to skip
            if (
                self.current_combo
                and self.current_combo.player in self.current_round.skip_votes
            ):
                self.current_combo = None

            await self.skip()


class DiscordUi:
    def __init__(self, channel, game):
        self.channel = channel
        self.game = game
        self.sample_message = None

        self.game.subscribe_to_signal("sample_skipped", self.sample_skipped)
        self.game.subscribe_to_signal("game_finished", self.game_finished)
        self.game.subscribe_to_signal("sample_timeout", self.sample_timeout)
        self.game.subscribe_to_signal("new_sample", self.new_sample)
        self.game.subscribe_to_signal("correct_guess", self.correct_guess)
        self.game.subscribe_to_signal("incorrect_guess", self.incorrect_guess)

    def get_ranking(self, scores):
        ranking = [
            item
            for item in sorted(scores.items(), key=lambda item: item[1], reverse=True)
        ]
        return [
            f"{symbol} - {name} - {score} pts"
            for symbol, (name, score) in zip(["????", "????", "????"], ranking)
        ]

    async def correct_guess(self, player, message, track_title):
        congrats_messages = ["yay", "correct", "nice", "good job", "????", "you rock"]
        congrats_message = random.choice(congrats_messages)
        embed = discord.Embed(
            title=f"It was **{track_title}**"
        )
        embed.add_field(
            name="**Leaderboard**", value="\n".join(self.get_ranking(self.game.scores)),
        )
        pts = self.game.current_combo.combo
        pts_description = "pt" if pts < 2 else "pts"
        asyncio.gather(
            message.add_reaction("???"),
            self.channel.send(
                f"@{player} {congrats_message}! You earn **{pts} {pts_description}**. Keep scoring to use your {min(self.game.current_combo.combo + 1, MAX_COMBO)}x multiplier!",
                embed=embed,
            ),
        )

    async def incorrect_guess(self, player, guess, message):
        await message.add_reaction("???")

    async def new_sample(self, sample_number):
        sample = self.game.current_round.sample
        filename = sample.image_url[sample.image_url.rfind("/") :]
        embed = discord.Embed(
            title="Guess the track and artist! ???", description="To skip it, react with ???.",
        )
        embed.set_footer(text=f"{sample_number} / {NB_SAMPLES}")
        self.sample_message = await self.channel.send(
            embed=embed,
            files=[discord.File(fp=io.BytesIO(sample.image_data), filename=filename,)],
        )
        await self.sample_message.add_reaction("???")

    async def sample_timeout(self):
        await self.channel.send(
            embed=discord.Embed(
                title="Time???s up! ???",
                # TODO: Set/use the correct sample field
                description=f"The track was **{self.game.current_round.sample.track_title}**.",
            )
        )

    async def game_finished(self):
        ranking = "\n".join(self.get_ranking(self.game.scores))
        embed = discord.Embed(
            title="Ranking", description=ranking if self.game.scores else "No scores!"
        )
        await self.channel.send("The music quiz is finished!", embed=embed)

        try:
            existing_stats = GameStats.load(get_stats_file_path(self.channel.id))
        except FileNotFoundError:
            stats = [self.game.stats]
        else:
            stats = existing_stats + [self.game.stats]

        if not os.path.exists(STATS_DIR):
            os.makedirs(STATS_DIR)

        with open(get_stats_file_path(self.channel.id), "w") as f:
            f.write(json.dumps([stat.asdict() for stat in stats]))

    async def sample_skipped(self):
        embed = discord.Embed(
            title="Sample skipped",
            description=f"The track was **{self.game.current_round.sample.track_title}**.",
        )
        await self.channel.send(embed=embed)


class QuizClient(discord.Client):
    def __init__(self):
        super().__init__()
        self.uis = {}

    def get_game(self, channel_id):
        return self.uis[channel_id].game

    async def on_ready(self):
        logger.info("Logged in as %s", self.user)

    async def start_game(self, channel):
        try:
            game = self.get_game(channel.id)
        except KeyError:
            game = None

        if game and game.status != GameStatus.IDLE:
            logger.error(
                "Tried to start a game on channel %s, but game status is %s!",
                channel.id,
                game.status,
            )
            return

        await channel.send(
            f"Get ready, a new game is about to start!"
        )

        game = Game()
        ui = DiscordUi(channel, game)

        self.uis[channel.id] = ui

        try:
            await game.game_loop()
        finally:
            logger.debug("Game loop is finished, cleaning up UI")
            del self.uis[channel.id]

    async def show_stats(self, channel):
        try:
            game_stats = GameStats.load(get_stats_file_path(channel.id))
        except FileNotFoundError:
            await channel.send(
                "No games played yet. Start a game with `@MusicQuiz start`!"
            )
            return

        table = Table(
            [
                Heading("#", justify=Justify.RIGHT),
                Heading("Player"),
                Heading("Games", justify=Justify.RIGHT),
                Heading("Correct guesses", justify=Justify.RIGHT),
                Heading("Total guesses", justify=Justify.RIGHT),
                Heading("Ratio (%)", justify=Justify.RIGHT),
                Heading("Max streak", justify=Justify.RIGHT),
                Heading("Avg reaction time (s)", justify=Justify.RIGHT),
            ]
        )
        player_stats = {}

        for game in game_stats:
            for player_id, stat in game.stats.items():
                player_stats.setdefault(player_id, []).append(
                    PlayerStat(
                        player_id=stat.player_id,
                        player_name=stat.player_name,
                        nb_guesses=stat.nb_guesses,
                        nb_samples_played=stat.nb_samples_played,
                        nb_correct_guesses=stat.nb_correct_guesses,
                        nb_skips=stat.nb_skips,
                        nb_aces=stat.nb_aces,
                        max_streak=stat.max_streak,
                        reaction_time=stat.reaction_time,
                        precision=stat.precision,
                    )
                )

        ranking = sorted(
            [sum(stats[1:], stats[0]) for stats in player_stats.values()],
            key=lambda item: (item.avg_correct_guesses_per_game, item.nb_games),
            reverse=True,
        )
        ranking = [stats for stats in ranking if stats.nb_games >= 10]

        for position, stat in enumerate(ranking[:10], 1):
            table.add_row(
                str(position),
                stat.player_name,
                str(stat.nb_games),
                str(stat.nb_correct_guesses),
                str(stat.nb_guesses),
                str(f"{stat.correct_guesses_ratio:.2f}"),
                str(stat.max_streak),
                str(f"{stat.reaction_time:.2f}"),
            )

        await channel.send(
            f"""
```
{str(table)}
```"""
        )

    async def on_reaction_add(self, reaction, user):
        if user.id == self.user.id:
            return

        try:
            ui = self.uis[reaction.message.channel.id]
        except KeyError:
            return

        logger.debug("Got reaction %s", reaction.emoji)
        if (
            ui.sample_message
            and ui.sample_message.id == reaction.message.id
            and reaction.emoji == "???"
        ):
            await ui.game.vote_skip(player_id=user.id, player_name=user.name)

    async def on_message(self, message):
        if message.author.id == self.user.id:
            return

        try:
            game = self.get_game(message.channel.id)
        except KeyError:
            game = None

        try:
            command = self.get_command(message)
        except ValueError as e:
            await message.channel.send(str(e))
            return

        if (
            command
            and command.type == CommandType.START
            and (not game or game.status == GameStatus.IDLE)
        ):
            await self.start_game(message.channel)
        elif command and command.type == CommandType.HELP:
            await message.channel.send(
                "Available commands are: start [easy|medium|hard]."
            )
        elif command and command.type == CommandType.STATS and not game:
            await self.show_stats(message.channel)
        elif game and game.status == GameStatus.WAITING_FOR_GUESSES:
            stripped_content = message.content.strip()
            if not (
                stripped_content.startswith("(") and stripped_content.endswith(")")
            ):
                await game.handle_guess(
                    player_name=message.author.name,
                    player_id=message.author.id,
                    guess=stripped_content,
                    message=message,
                )

    def get_command(self, message):
        mention = f"<@!{self.user.id}>"

        if not message.content.startswith(mention):
            return None

        command = message.content[len(mention) :].strip()
        try:
            command_type, *args = command.split(" ")
        except ValueError:
            command_type = command
            args = []

        return Command(type=CommandType(command_type), args=args)


def main():
    env_vars = {
        var_name: os.environ.get(var_name)
        for var_name in ("DISCORD_TOKEN",)
    }
    missing_vars = {var_name for var_name, value in env_vars.items() if not value}

    if missing_vars:
        missing_vars_str = ", ".join(missing_vars)
        sys.stderr.write(
            f"The following environment variables are missing: {missing_vars_str}. Please set them and re-run the program."
        )
        sys.exit(1)

    client = QuizClient()

    client.run(env_vars["DISCORD_TOKEN"])


if __name__ == "__main__":
    main()
