import { describe, expect, it } from "vitest";
import { licencePage, licenceTerms } from "./licences";

describe("licences with terms", () => {
  it("FY-WEB-174 marks OpenRAIL, Gemma and Llama licences, not permissive ones, and links the licence's page", () => {
    for (const id of ["openrail++", "creativeml-openrail-m", "OpenRAIL"]) expect(licenceTerms(id)).toMatch(/^OpenRAIL: .*forbids some uses/);
    expect(licenceTerms("gemma")).toMatch(/prohibited use policy/);
    expect(licenceTerms("llama3.2")).toMatch(/700 million/);
    for (const id of ["apache-2.0", "mit", "bsd-3-clause", "", null, undefined, "other"]) expect(licenceTerms(id)).toBeNull();
    expect(licencePage("hf://stabilityai/stable-diffusion-xl-base-1.0")).toBe("https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0");
    // a conversion: the licence lives on the original model's page
    expect(licencePage("hf://second-state/stable-diffusion-v1-5-GGUF", "hf://stable-diffusion-v1-5/stable-diffusion-v1-5"))
      .toBe("https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5");
    expect(licencePage("/srv/models/x")).toBeNull();
  });
});
