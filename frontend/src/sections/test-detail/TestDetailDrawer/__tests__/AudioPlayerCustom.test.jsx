import { beforeEach, describe, expect, it, vi } from "vitest";
import { render } from "src/utils/test-utils";

const captured = { trackUrls: null };

// Mutable so a case can hand back real split channels; hoisted because the
// vi.mock factory below is lifted above this file's other statements.
const { stereo } = vi.hoisted(() => ({
  stereo: { assistantUrl: "", customerUrl: "", loading: false, error: null },
}));

vi.mock("src/components/iconify", () => ({
  default: ({ icon, ...props }) => (
    <span data-testid="iconify" data-icon={icon} {...props} />
  ),
}));

vi.mock("src/hooks/use-stereo-channels", () => ({
  default: () => stereo,
}));

vi.mock("src/components/multi-track-audio-player/MultiTrackAudioPlayer", () => ({
  default: (props) => {
    captured.trackUrls = props.trackUrls;
    return <div data-testid="multi-track" />;
  },
  MemoizedBarsIcon: () => <span data-testid="bars" />,
}));

// Imported after the mocks above so the component picks them up.
import { StereoMultiTrackPlayer } from "../AudioPlayerCustom";

const COMBINED = "https://example.test/recording.wav";

describe("StereoMultiTrackPlayer track selection", () => {
  beforeEach(() => {
    captured.trackUrls = null;
    Object.assign(stereo, {
      assistantUrl: "",
      customerUrl: "",
      loading: false,
      error: null,
    });
  });

  it("splits a stereo recording into two channel tracks", () => {
    Object.assign(stereo, {
      assistantUrl: "blob:assistant",
      customerUrl: "blob:customer",
    });

    render(
      <StereoMultiTrackPlayer
        recordings={{ stereo: "https://example.test/stereo.wav" }}
        id="call-0"
      />,
    );

    expect(captured.trackUrls).toHaveLength(2);
    expect(captured.trackUrls.map((t) => t.url)).toEqual([
      "blob:customer",
      "blob:assistant",
    ]);
  });

  it("renders the single mix when a provider only exposes a combined recording", () => {
    // The player gates readiness on every track loading, so handing it two
    // undefined channel tracks leaves it painting waveforms forever.
    render(
      <StereoMultiTrackPlayer recordings={{ combined: COMBINED }} id="call-1" />,
    );

    expect(captured.trackUrls).toHaveLength(1);
    expect(captured.trackUrls[0].url).toBe(COMBINED);
    expect(captured.trackUrls.every((t) => Boolean(t.url))).toBe(true);
  });

  it("still splits into customer and assistant rows when channels exist", () => {
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
