import { describe, expect, it, vi } from "vitest";
import { render } from "src/utils/test-utils";

const captured = { trackUrls: null };

vi.mock("src/components/iconify", () => ({
  default: ({ icon, ...props }) => (
    <span data-testid="iconify" data-icon={icon} {...props} />
  ),
}));

vi.mock("src/hooks/use-stereo-channels", () => ({
  default: () => ({
    assistantUrl: "",
    customerUrl: "",
    loading: false,
    error: null,
  }),
}));

vi.mock("src/components/multi-track-audio-player/MultiTrackAudioPlayer", () => ({
  default: (props) => {
    captured.trackUrls = props.trackUrls;
    return <div data-testid="multi-track" />;
  },
  MemoizedBarsIcon: () => <span data-testid="bars" />,
}));

// eslint-disable-next-line import/first
import { StereoMultiTrackPlayer } from "../AudioPlayerCustom";

const COMBINED = "https://example.test/recording.wav";

describe("StereoMultiTrackPlayer track selection", () => {
  it("renders the single mix when a provider only exposes a combined recording", () => {
    // The player gates readiness on every track loading, so handing it two
    // undefined channel tracks leaves it painting waveforms forever.
    captured.trackUrls = null;
    render(
      <StereoMultiTrackPlayer recordings={{ combined: COMBINED }} id="call-1" />,
    );

    expect(captured.trackUrls).toHaveLength(1);
    expect(captured.trackUrls[0].url).toBe(COMBINED);
    expect(captured.trackUrls.every((t) => Boolean(t.url))).toBe(true);
  });

  it("still splits into customer and assistant rows when channels exist", () => {
    captured.trackUrls = null;
    render(
      <StereoMultiTrackPlayer
        recordings={{
          combined: COMBINED,
          assistant: "https://example.test/assistant.wav",
          customer: "https://example.test/customer.wav",
        }}
        id="call-2"
      />,
    );

    expect(captured.trackUrls).toHaveLength(2);
    expect(captured.trackUrls.map((t) => t.name)).toEqual([
      "Customer Audio",
      "Assistant Audio",
    ]);
  });
});
