// 1.5.5 (#143): what a catalog model does with images given to it, as chips beside its task: "Edits images" and
// "Image to video" (the model page, the catalog).
import { IconMovie, IconWand } from "@tabler/icons-react";

import { abilities } from "../lib/imageEdit";
import type { CatalogModel } from "../lib/types";

export function AbilityChips({ m }: { m: Pick<CatalogModel, "task" | "image" | "video"> }) {
  return (
    <>
      {abilities(m).map((a) => (
        <span key={a.key} className="chip chip-tone-run" title={a.title}>
          {a.key === "i2v" ? <IconMovie size={11} aria-hidden="true" /> : <IconWand size={11} aria-hidden="true" />} {a.text}
        </span>
      ))}
    </>
  );
}
