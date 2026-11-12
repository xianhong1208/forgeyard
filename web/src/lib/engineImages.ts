// Engine images on a machine that cannot reach the registry: pulled and saved on one that can,
// loaded there. A pinned image (repo:tag@sha256:…) is pulled by its digest, so the file holds exactly
// the pinned image, and saved under its tag: `docker load` keeps the tag, and the agent finds the
// loaded copy by that tag and checks it against the ids recorded for the image.

export type EngineCarry = { file: string; save: string; load: string };

// "ghcr.io/ggml-org/llama.cpp:server-vulkan-b12242@sha256:…" -> name, tag ref and digest ref.
export function splitImage(image: string): { name: string; tagRef: string | null; digestRef: string | null } {
  const at = image.indexOf("@");
  const ref = at >= 0 ? image.slice(0, at) : image;
  const digest = at >= 0 ? image.slice(at + 1) : null;
  const last = ref.split("/").pop() ?? ref;
  const name = last.includes(":") ? ref.slice(0, ref.lastIndexOf(":")) : ref;
  return { name, tagRef: last.includes(":") ? ref : null, digestRef: digest ? `${name}@${digest}` : null };
}

export function engineImageFile(image: string): string {
  const { name, tagRef } = splitImage(image);
  const base = (name.split("/").pop() ?? "image") + (tagRef ? `-${tagRef.slice(tagRef.lastIndexOf(":") + 1)}` : "");
  return `${base.replace(/[^A-Za-z0-9._-]/g, "-")}.tar.gz`;
}

export function engineCarry(image: string): EngineCarry {
  const { tagRef, digestRef } = splitImage(image);
  const file = engineImageFile(image);
  const lines: string[] = [];
  if (digestRef) {
    lines.push(`sudo docker pull ${digestRef}`);
    if (tagRef) lines.push(`sudo docker tag ${digestRef} ${tagRef}`);
  } else {
    lines.push(`sudo docker pull ${image}`);
  }
  lines.push(`sudo docker save ${tagRef ?? digestRef ?? image} | gzip > ${file}`);
  return { file, save: lines.join(" && \\\n  "), load: `gunzip -c ${file} | sudo docker load` };
}
