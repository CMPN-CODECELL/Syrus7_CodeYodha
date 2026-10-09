import { Instrument_Serif } from "next/font/google";

/** Editorial serif for landing and auth headlines; exposed as the CSS variable --font-display. */
export const displayFont = Instrument_Serif({
  subsets: ["latin"],
  weight: "400",
  style: ["normal", "italic"],
  variable: "--font-display",
  display: "swap",
});
