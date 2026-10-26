"""Curated themed word lists for the "cool words" candidate mode.

Short, punchy, letters-only words people actually want in a handle. Single
words are mostly taken already; two-word combos ("voidfox", "lunarwolf") are
where the available names are.
"""
from __future__ import annotations

THEMES: dict[str, tuple[str, str]] = {
    # name: (label, space-separated words)
    "space": ("Space", """
        nova astro orbit comet lunar solar nebula cosmo cosmic galaxy star stellar vega orion
        zenith eclipse void quasar pulsar meteor astral aurora halo titan rocket apollo venus
        mars saturn pluto sirius polaris moon sun sky cosmos ion photon gravity warp nadir
        apex ether starlight orbital"""),
    "nature": ("Nature", """
        ember ash fern moss cedar pine oak willow maple river brook stone flint frost storm
        rain thunder blaze flame wild wave tide coral reef dune mesa canyon cliff peak summit
        grove glade meadow bloom petal lotus ivy sage thorn birch aspen cove lagoon mist fog
        dusk dawn ocean forest"""),
    "myth": ("Mythology", """
        zeus odin thor loki freya atlas hermes ares athena artemis hydra kraken phoenix
        griffin sphinx valkyrie oracle siren nyx eros helios kronos anubis osiris merlin
        avalon olympus midgard asgard rune myth legend saga fable chimera cerberus pegasus
        medusa golem djinn wyvern dragon titan"""),
    "dark": ("Dark", """
        shadow noir raven onyx obsidian phantom ghost specter wraith rogue venom havoc chaos
        abyss crypt grim vex hex omen ruin dread gloom midnight night nocturne shade sable
        vandal rebel outlaw exile haze vortex cipher enigma reaper hollow"""),
    "animals": ("Animals", """
        wolf fox lynx hawk falcon raven crow owl viper cobra tiger panther jaguar puma lion
        bear orca shark eagle bison stag otter koi panda gecko mantis hornet wasp moth swan
        heron crane sparrow finch wren jay coyote hyena rhino mako manta raptor drake kitsune"""),
    "tech": ("Tech", """
        pixel byte glitch neon cyber data code chrome circuit binary vector matrix nexus proxy
        kernel macro nano quantum laser sonic turbo synth drone droid robo mecha techno logic
        flux volt static signal echo delta alpha omega sigma beta gamma zero null root vapor"""),
    "colours": ("Colours", """
        crimson scarlet ruby amber gold ivory jade emerald cobalt azure indigo violet lilac teal
        cyan ebony silver copper bronze rose blush peach mint olive navy sapphire opal pearl
        rust sepia mauve plum berry cherry lemon"""),
    "vibes": ("Vibes", """
        chill vibe lucid dream mellow cozy sunny lofi retro vintage golden velvet silk honey
        sugar candy peachy bubbly glow shine spark sparkle glitter dreamy hazy wavy drift float
        bliss zen calm serene aura mood soul spirit karma lucky magic wonder muse"""),
}


def theme_words(name: str) -> list[str]:
    return list(dict.fromkeys(THEMES[name][1].split()))


def words_for(themes: list[str]) -> list[str]:
    out: list[str] = []
    for t in themes:
        out += theme_words(t)
    return list(dict.fromkeys(out))


def catalogue() -> list[dict]:
    return [
        {"name": name, "label": label, "count": len(theme_words(name)), "examples": theme_words(name)[:4]}
        for name, (label, _) in THEMES.items()
    ]
