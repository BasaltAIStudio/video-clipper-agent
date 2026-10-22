"""Test the new heuristic scorer."""
import sys
sys.path.insert(0, ".")

from agent.heuristic import score_segments, score_sentence

# Build a mock transcript with word-level data
transcript = {
    "duration": 120.0,
    "segments": [
        {
            "start": 5.0,
            "end": 18.0,
            "text": "The secret is that nobody talks about it. Actually, here's the thing nobody's talking about.",
            "words": [{"start": 5.0, "end": 6.5, "word": "The"}, {"start": 6.5, "end": 7.0, "word": "secret"}, {"start": 7.0, "end": 7.5, "word": "is"}, {"start": 7.5, "end": 8.0, "word": "that"}, {"start": 8.0, "end": 9.0, "word": "nobody"}, {"start": 9.0, "end": 9.5, "word": "talks"}, {"start": 9.5, "end": 10.0, "word": "about"}, {"start": 10.0, "end": 10.5, "word": "it."}, {"start": 10.5, "end": 11.0, "word": "Actually,"}, {"start": 11.0, "end": 11.5, "word": "here's"}, {"start": 11.5, "end": 12.0, "word": "the"}, {"start": 12.0, "end": 12.5, "word": "thing"}, {"start": 12.5, "end": 13.0, "word": "nobody's"}, {"start": 13.0, "end": 13.5, "word": "talking"}, {"start": 13.5, "end": 14.0, "word": "about"}, {"start": 14.0, "end": 14.5, "word": "it."}],
        },
        {
            "start": 20.0,
            "end": 25.0,
            "text": "Here's the key: use a 9:16 aspect ratio for shorts.",
            "words": [{"start": 20.0, "end": 20.5, "word": "Here's"}, {"start": 20.5, "end": 21.0, "word": "the"}, {"start": 21.0, "end": 21.5, "word": "key:"}, {"start": 21.5, "end": 22.0, "word": "use"}, {"start": 22.0, "end": 22.5, "word": "a"}, {"start": 22.5, "end": 23.0, "word": "9:16"}, {"start": 23.0, "end": 23.5, "word": "aspect"}, {"start": 23.5, "end": 24.0, "word": "ratio"}, {"start": 24.0, "end": 24.5, "word": "for"}, {"start": 24.5, "end": 25.0, "word": "shorts."}],
        },
        {
            "start": 30.0,
            "end": 40.0,
            "text": "This is a very long segment with no viral cues at all, just filler.",
            "words": [{"start": 30.0, "end": 30.5, "word": "This"}, {"start": 30.5, "end": 31.0, "word": "is"}, {"start": 31.0, "end": 31.5, "word": "a"}, {"start": 31.5, "end": 32.0, "word": "very"}, {"start": 32.0, "end": 32.5, "word": "long"}, {"start": 32.5, "end": 33.0, "word": "segment"}, {"start": 33.0, "end": 33.5, "word": "with"}, {"start": 33.5, "end": 34.0, "word": "no"}, {"start": 34.0, "end": 34.5, "word": "viral"}, {"start": 34.5, "end": 35.0, "word": "cues"}, {"start": 35.0, "end": 35.5, "word": "at"}, {"start": 35.5, "end": 36.0, "word": "all,"}, {"start": 36.0, "end": 36.5, "word": "just"}, {"start": 36.5, "end": 37.0, "word": "filler."}],
        },
        {
            "start": 50.0,
            "end": 55.0,
            "text": "You won't believe what happened next! This was shocking and unbelievable.",
            "words": [{"start": 50.0, "end": 50.5, "word": "You"}, {"start": 50.5, "end": 51.0, "word": "won't"}, {"start": 51.0, "end": 51.5, "word": "believe"}, {"start": 51.5, "end": 52.0, "word": "what"}, {"start": 52.0, "end": 52.5, "word": "happened"}, {"start": 52.5, "end": 53.0, "word": "next!"}, {"start": 53.0, "end": 53.5, "word": "This"}, {"start": 53.5, "end": 54.0, "word": "was"}, {"start": 54.0, "end": 54.5, "word": "shocking"}, {"start": 54.5, "end": 55.0, "word": "and"}, {"start": 55.0, "end": 55.5, "word": "unbelievable."}],
        },
    ],
}

results = score_segments(transcript, num_clips=3)
print(f"Found {len(results)} highlights:")
for i, h in enumerate(results, 1):
    print(f"  #{i}: [{h['start']:.0f}s -> {h['end']:.0f}s] overall={h['overall']}")
    print(f"      text: {h['text'][:80]}...")

# Test individual score_sentence
s, o = score_sentence(transcript["segments"][0])
print(f"\nscore_sentence test: overall={o}, hook={s['hook']:.2f}, arc={s['arc']:.2f}")
assert isinstance(o, float) and 0 <= o <= 100, "score out of range"
assert s['hook'] > 0.5, f"hook should be high for 'The secret' text, got {s['hook']}"

# Test LLM provider toggle
from agent.config import USE_LLM, LLM_PROVIDER
print(f"\nLLM_PROVIDER={LLM_PROVIDER}, USE_LLM={USE_LLM}")
assert LLM_PROVIDER in ("openrouter", "nvidia_nim", "none"), f"Unexpected LLM_PROVIDER: {LLM_PROVIDER}"
assert isinstance(USE_LLM, bool), "USE_LLM should be bool"

print("\n=== ALL TESTS PASSED ===")
