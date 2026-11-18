// @vitest-environment jsdom
// Text classification (1.4.4, #144): the Playground's panel, the example call, and where the kind shows
// (FY-WEB-490 to 494).
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { MODEL_KIND, MODEL_KIND_ORDER } from "../lib/fleet";
import { API_KIND, snippets } from "../lib/gateway";
import { API_LABEL, TASK_LABEL, recipeDefaults } from "../lib/models";
import { pickerGroups, routeKind } from "../lib/playground";
import type { Deployment, Recipe, RouteInfo } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const UP = [{ replica: 0, machine: "a100", state: "healthy", ready: true, url: "http://10.0.0.5:31000/" }];
const route = (served_name: string, api: string, id: number) => ({ local_only: false, spill_on_long_prompt: false, wait_ms: 0, kind: "local",
  has_local: true, has_cloud: false, targets: [], usable: true, upstream_model: null, endpoints: UP, id, served_name, api, deployment_id: id }) as unknown as RouteInfo;
const STARS = ["1 star", "2 stars", "3 stars", "4 stars", "5 stars"];
const TOXIC = ["toxic", "severe_toxic", "obscene", "threat", "insult", "identity_hate"];
const dep = (id: number, task: string, classify: unknown = null) => ({ id, model: { task, modalities: [], classify } }) as unknown as Deployment;
const DEPLOYMENTS = [
  dep(1, "classify", { architecture: "BertForSequenceClassification", labels: 5, label_names: STARS }),
  dep(2, "classify", { architecture: "BertForSequenceClassification", labels: 6, label_names: TOXIC, problem: "multi_label_classification" }),
  dep(3, "classify", null),
  dep(4, "chat"),
];
const ROUTES = [route("stars", "classify", 1), route("toxic", "classify", 2), route("local-cls", "classify", 3), route("qwen3-4b", "openai-chat", 4)];

function server(answer: (body: { model: string; input: string[] }) => { status: number; body: unknown }) {
  const sent: { url: string; body: { model: string; input: string[] } }[] = [];
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/playground/classify") {
      const body = JSON.parse(String(init?.body));
      sent.push({ url, body });
      const a = answer(body);
      return new Response(JSON.stringify(a.body), { status: a.status });
    }
    const body = url === "/api/auth/state" ? { needs_setup: false, registration: "off", user: OWNER }
      : url === "/api/routes" ? { cloud: "off", is_admin: true, routes: ROUTES }
        : url === "/api/deployments" ? { deployments: DEPLOYMENTS, server_time: "", can_manage: true } : undefined;
    return new Response(JSON.stringify(body ?? {}), { status: body === undefined ? 404 : 200 });
  });
  vi.stubGlobal("fetch", fetch);
  return { fetch, sent };
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

const probs = (n: number, top: number, p: number) => Array.from({ length: n }, (_, i) => (i === top ? p : (1 - p) / (n - 1)));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Playground: text classification (1.4.4)", () => {
  it("FY-WEB-490 one text per line goes to /classify; each comes back with its label and every label's bar, most likely first, named from the model's config", async () => {
    const { sent } = server((b) => ({ status: 200, body: { model: b.model, usage: { prompt_tokens: 17, total_tokens: 17 },
      data: b.input.map((_, i) => ({ index: i, label: i === 0 ? "5 stars" : "1 star", probs: probs(5, i === 0 ? 4 : 0, 0.8), num_classes: 5 })) } }));
    mount("/playground?model=stars");
    expect(await screen.findByRole("heading", { name: "Text classification" })).toBeTruthy();
    expect(screen.getByText(/5 labels: 1 star, 2 stars, 3 stars, 4 stars, 5 stars\./)).toBeTruthy();
    expect(screen.getByText(/add up to 1/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Texts, one per line"), { target: { value: "Great phone!\n\n  \nBroke on day one." } });
    fireEvent.click(screen.getByRole("button", { name: "Classify" }));
    const results = await screen.findByRole("list", { name: "Labels" });
    expect(sent).toEqual([{ url: "/api/playground/classify", body: { model: "stars", input: ["Great phone!", "Broke on day one."] } }]);
    const items = within(results).getAllByRole("listitem").filter((li) => li.className === "cls-item");
    expect(items).toHaveLength(2);
    expect(items[0].textContent).toContain("Great phone!");
    expect(items[0].querySelector(".cls-top strong")?.textContent).toBe("5 stars");
    const bars = within(items[0]).getByRole("list", { name: "Probabilities for text 1" });
    const names = within(bars).getAllByRole("listitem").map((li) => li.querySelector(".cls-name")?.textContent);
    expect(names[0]).toBe("5 stars");
    expect(names).toHaveLength(5);
    expect((within(bars).getAllByRole("listitem")[0].querySelector(".cls-bar span") as HTMLElement).style.width).toBe("80%");
    expect(items[1].querySelector(".cls-top strong")?.textContent).toBe("1 star");
    expect(screen.getByText(/17 tokens/)).toBeTruthy();
    // Five labels: nothing folded.
    expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull();
  });

  it("FY-WEB-491 more than five labels fold behind 'Show all'; a multi-label model says its probabilities stand alone", async () => {
    server((b) => ({ status: 200, body: { data: b.input.map((_, i) => ({ index: i, label: "insult", probs: [0.7, 0.05, 0.6, 0.01, 0.9, 0.02], num_classes: 6 })) } }));
    mount("/playground?model=toxic");
    expect(await screen.findByText(/6 labels: toxic, severe_toxic, obscene, threat, insult, identity_hate\. Each label is scored on its own \(multi-label\)/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Texts, one per line"), { target: { value: "you are an idiot" } });
    fireEvent.click(screen.getByRole("button", { name: "Classify" }));
    const bars = await screen.findByRole("list", { name: "Probabilities for text 1" });
    expect(within(bars).getAllByRole("listitem").map((li) => li.querySelector(".cls-name")?.textContent))
      .toEqual(["insult", "toxic", "obscene", "severe_toxic", "identity_hate"]);
    fireEvent.click(screen.getByRole("button", { name: "Show all 6 labels" }));
    expect(within(bars).getAllByRole("listitem")).toHaveLength(6);
    fireEvent.click(screen.getByRole("button", { name: "Show the top five" }));
    expect(within(bars).getAllByRole("listitem")).toHaveLength(5);
  });

  it("FY-WEB-492 without label names (a local model) the engine's label names the top one and the rest are numbered; an error is shown as the gateway says it", async () => {
    let fail = false;
    server((b) => (fail ? { status: 400, body: { error: { message: "'input' has an empty text.", type: "invalid_request_error", code: "invalid_request" } } }
      : { status: 200, body: { data: b.input.map((_, i) => ({ index: i, label: "POSITIVE", probs: [0.25, 0.75], num_classes: 2 })) } }));
    mount("/playground?model=local-cls");
    await screen.findByRole("heading", { name: "Text classification" });
    fireEvent.change(screen.getByLabelText("Texts, one per line"), { target: { value: "fine" } });
    fireEvent.click(screen.getByRole("button", { name: "Classify" }));
    const bars = await screen.findByRole("list", { name: "Probabilities for text 1" });
    expect(within(bars).getAllByRole("listitem").map((li) => li.querySelector(".cls-name")?.textContent)).toEqual(["POSITIVE", "label 0"]);
    fail = true;
    fireEvent.click(screen.getByRole("button", { name: "Classify" }));
    await waitFor(() => expect(screen.getByText(/has an empty text/)).toBeTruthy());
    expect(screen.queryByRole("list", { name: "Labels" })).toBeNull();
  });

  it("FY-WEB-493 the kind: its label, endpoint and icon group in the picker, the route's kind, and its place after Rerank", () => {
    expect(TASK_LABEL.classify).toBe("Text classification");
    expect(MODEL_KIND.classify).toEqual({ label: "Text classification", api: "/v1/classify", hint: "Labels a text: sentiment, language, toxicity…" });
    expect(MODEL_KIND_ORDER.indexOf("classify")).toBe(MODEL_KIND_ORDER.indexOf("rerank") + 1);
    expect(API_LABEL.classify).toBe("/v1/classify");
    expect(API_KIND.classify).toBe("text classification");
    expect(routeKind(ROUTES[0], DEPLOYMENTS)).toBe("classify");
    expect(pickerGroups(ROUTES, DEPLOYMENTS).map(([k, l]) => [k, l.length])).toEqual([["chat", 1], ["classify", 3]]);
  });

  it("FY-WEB-494 the example call (curl and httpx, with the CA when there is one), and a context default no longer than the model's", () => {
    const s = snippets("https://fy.example/v1", "fyk_x", "stars", "classify");
    expect(s.curl).toContain("curl https://fy.example/v1/classify");
    expect(s.curl).toContain(`"input": ["I love this!", "This is terrible."]`);
    expect(s.python).toContain(`httpx.post("https://fy.example/v1/classify"`);
    expect(s.python).not.toContain("verify=ca");
    const ca = snippets("https://fy.example/v1", "fyk_x", "stars", "classify", "https://fy.example/trust");
    expect(ca.curl).toContain("--cacert forgeyard-ca.pem");
    expect(ca.python).toContain("verify=ca");
    const recipe = { params: { context: { type: "int", default: 8192, min: 256 }, concurrency: { type: "int", default: 32 } } } as unknown as Recipe;
    expect(recipeDefaults(recipe)).toEqual({ context: "8192", concurrency: "32" });
    expect(recipeDefaults(recipe, 512)).toEqual({ context: "512", concurrency: "32" });
    expect(recipeDefaults(recipe, 100)).toEqual({ context: "256", concurrency: "32" });
    expect(recipeDefaults(recipe, 32768)).toEqual({ context: "8192", concurrency: "32" });
    expect(recipeDefaults(recipe, null)).toEqual({ context: "8192", concurrency: "32" });
  });
});
