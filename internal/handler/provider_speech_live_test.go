package handler_test

import "testing"

func TestSpeechProvidersHTTPCLIAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	t.Run("MiniMax/tts", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalMedia(t, f, "MiniMax", "speech-02-hd", instance, "tts") })
		}
	})
	t.Run("FishAudio/tts", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalMedia(t, f, "FishAudio", "s1", instance, "tts") })
		}
	})
	t.Run("FishAudio/speech2text", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalMedia(t, f, "FishAudio", "transcribe-1", instance, "speech2text") })
		}
	})
	t.Run("OpenRouter/tts", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalMedia(t, f, "OpenRouter", "openai/gpt-audio-mini", instance, "tts")
			})
		}
	})
	t.Run("OpenRouter/speech2text", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				exerciseCanonicalMedia(t, f, "OpenRouter", "openai/whisper-large-v3", instance, "speech2text")
			})
		}
	})
}
