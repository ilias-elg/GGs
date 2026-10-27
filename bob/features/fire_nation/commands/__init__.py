"""
Slash commands and gateway listeners for the Fire Nation features, one cog per
area. Every rule lives in the feature modules (merit.py, ranks.py, voice.py);
the handlers here only parse options, call those, and format the reply.
"""

from .admin import AdminCommands
from .merits import MeritCommands
from .voice import VoiceCommands

COGS = (MeritCommands, AdminCommands, VoiceCommands)
