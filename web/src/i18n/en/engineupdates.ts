// Newer versions of the built-in engines (1.5.2, #95; components/EngineUpdates.tsx): the check on Models → Engines,
// the badges on the engine cards and the Newer versions section of an engine's page. The why.* keys are the server's
// reason codes (forgeyard/models/engineversions.py), each with the same words as the server's English.
export default {
  "engineupdates.check": "Check for newer versions",
  "engineupdates.checkAgain": "Check again",
  "engineupdates.checking": "Checking… (up to a minute and a half)",
  "engineupdates.checkTitle":
    "Ask Docker Hub, GHCR and GitHub now, through this server's network rules, for newer versions of every built-in engine",
  "engineupdates.lastChecked": "Last checked {when} by {name}",
  "engineupdates.never": "Not checked for newer versions yet.",
  "engineupdates.neverAsk": "Not checked for newer versions yet: an admin can check.",
  "engineupdates.checked": "Checked: {count} with a newer version.",
  "engineupdates.checkedNone": "Checked: every built-in engine is up to date.",
  "engineupdates.help": "Newer engine versions",

  // The badge on an engine card
  "engineupdates.badge.newer": "{version} available",
  "engineupdates.badge.newer_build": "Newer build",
  "engineupdates.badge.up_to_date": "Up to date",
  "engineupdates.badge.partly_checked": "Partly checked",
  "engineupdates.badge.could_not_check": "Could not check",
  "engineupdates.badgeTitle": "Newer versions: {list}",

  // The engine page's section
  "engineupdates.title": "Newer versions",
  "engineupdates.note":
    "Stable releases published as images, from this engine's own repositories only. Nothing changes until you choose: the built-in engine stays as it is. A newer version may change flags, defaults and memory use; Forgeyard's estimates were measured on {version}.",
  "engineupdates.upToDate": "Up to date: {version} is the newest stable image.",
  "engineupdates.upToDateBuild": "Up to date: every image tag still points at the pinned build.",
  "engineupdates.upToDatePlain": "Up to date.",
  "engineupdates.couldNot": "Could not check:",
  "engineupdates.partly": "Some of its images could not be checked:",
  "engineupdates.released": "released {date}",
  "engineupdates.built": "built {date}",
  "engineupdates.notes": "Release notes",
  "engineupdates.covered": "{count} of {total} variants included",
  "engineupdates.col.variant": "Variant",
  "engineupdates.col.version": "In this version",
  "engineupdates.col.image": "Image or archive",
  "engineupdates.included": "included",
  "engineupdates.leftOut": "left out",
  "engineupdates.archive": "release archive (SHA-256)",
  "engineupdates.mentions": "Its release notes mention flags or settings this engine uses:",
  "engineupdates.mentionsNone": "Its release notes mention none of the flags this engine passes.",
  "engineupdates.lines.one": "{count} line",
  "engineupdates.lines.other": "{count} lines",

  // Reasons (the server's codes)
  "engineupdates.why.no_image": "No {tag} image in {repo}; this variant's images stop at {last}.",
  "engineupdates.why.no_image_any": "No {tag} image in {repo}.",
  "engineupdates.why.tag_unversioned": "Its image tag carries no version, so it has no newer one to offer.",
  "engineupdates.why.registry_error": "Could not list its tags: {error}",
  "engineupdates.why.digest_failed": "Could not read the digest of {tag}: {error}",
  "engineupdates.why.installed_command": "It runs a command installed on the machine; Forgeyard does not update it.",
  "engineupdates.why.not_github": "Its archive is not a GitHub release download, so newer ones cannot be found.",
  "engineupdates.why.release_unknown": "Could not tell which GitHub release holds this version's archive.",
  "engineupdates.why.github_error": "GitHub: {error}",
  "engineupdates.why.no_release": "GitHub has no release {release}.",
  "engineupdates.why.no_archive": "Release {release} has no {archive}, the archive the built-in uses.",
  "engineupdates.why.no_archive_similar":
    "Release {release} has no {archive}, the archive the built-in uses; it has {similar}, built for another system.",
  "engineupdates.why.no_sha256": "GitHub publishes no SHA-256 for {archive}, so it cannot be pinned.",
  "engineupdates.why.other_build": "Its tag points at another newer build.",
  "engineupdates.why.not_newer": "Its tag moved, but to an image made before the pinned one.",
  "engineupdates.why.not_moved": "Its tag still points at the pinned build.",
  "engineupdates.why.no_notes": "No GitHub release found for it, so no release notes were read.",
  "engineupdates.why.own_image": "Forgeyard's own image: it moves with Forgeyard's releases.",
  "engineupdates.why.release_without_image":
    "GitHub has a release {version} with no image yet: it is offered once its images are published.",
} as const;
