// Try this version (1.5.2, #95 PR 2; components/EngineTry.tsx): the button on each newer version, its dialog, and the
// Copy section of a copy's page. The signature and error keys follow the server's codes
// (forgeyard/models/signatures.py, forgeyard/models/enginetry.py).
export default {
  "enginetry.try": "Try this version",
  "enginetry.tryTitle": "Make a custom engine from {name} with the images of {version}, pinned by digest",
  "enginetry.dlgTitle": "Try {title} {version}",
  "enginetry.lead":
    "Forgeyard makes a custom engine: a copy of {name} with the images of {version}, each pinned by its digest. The built-in {name} stays as it is, and no deployment changes.",
  "enginetry.name": "Name of the copy",
  "enginetry.nameHint": "Lowercase letters, digits and dashes. Deployments pick an engine by this name.",
  "enginetry.variants": "Variants",
  "enginetry.included": "included",
  "enginetry.leftOut": "left out",
  "enginetry.leftOutNote": "Machines that need a variant left out keep using the built-in {name}.",
  "enginetry.archiveLine": "{archive} (SHA-256 {sha})",
  "enginetry.sigTitle": "Signatures",
  "enginetry.signed":
    "{repository} signs its images and release archives (GitHub artifact attestations). Forgeyard verifies each one now; if a signature does not verify, no copy is made.",
  "enginetry.unsigned": "{text} The copy is pinned by digest only.",
  "enginetry.unsigned.vllm": "vLLM publishes no signatures or attestations for its images.",
  "enginetry.unsigned.vllm-omni": "vLLM-Omni publishes no signatures or attestations for its images.",
  "enginetry.unsigned.speaches": "speaches publishes no signatures or attestations for its images.",
  "enginetry.unsigned.stable-diffusion.cpp":
    "stable-diffusion.cpp publishes only an unsigned build record (BuildKit provenance) with its images: there is no signature to verify.",
  "enginetry.warn":
    "A new version can change flags, defaults and memory use. The copy keeps {name}'s arguments as they are, and Forgeyard's estimates were measured on {from}. Try it on one deployment before moving others.",
  "enginetry.mentions": "Its release notes mention flags or settings this engine uses: {list}.",
  "enginetry.notes": "Read the release notes",
  "enginetry.make": "Make the copy",
  "enginetry.making": "Making the copy… (checking digests and signatures)",
  "enginetry.nothing": "No variant has an image of this version, so there is nothing to copy.",
  "enginetry.done": "Made {name}, a copy of {from} for {version}.",
  "enginetry.open": "Go to {name}",
  "enginetry.close": "Close",
  "enginetry.help": "Try a newer version of an engine",
  "enginetry.unverifiableTitle": "The signatures could not be checked",
  "enginetry.unverifiableWarn":
    "You can still make the copy, but Forgeyard will not have checked who built its images: it is marked Not verified wherever its signatures are shown, and the audit log records that you chose this.",
  "enginetry.makeUnverified": "Create it without verification",
  "enginetry.notVerifiedChip": "Not verified",
  "enginetry.notVerifiedTitle": "Not verified: {reason} (made anyway by {who})",
  "enginetry.notVerifiedNote":
    "Its signatures were not verified: they could not be checked when the copy was made, and {who} made it anyway.",

  // A copy's page
  "enginetry.copyTitle": "Copy of {name}",
  "enginetry.copyLead":
    "Made from the built-in {link} ({from}) for {version} with “Try this version” on {date} by {who}. Not tested by Forgeyard. The built-in {name} stays as it is: a deployment uses this copy only when you choose it.",
  "enginetry.copyLeadManual":
    "Based on the built-in {link}: the catalog's engine arguments for {name} also apply here. The built-in {name} stays as it is.",
  "enginetry.col.variant": "Variant",
  "enginetry.col.pinned": "Pinned",
  "enginetry.col.signature": "Signature",
  "enginetry.verified": "verified",
  "enginetry.unsignedChip": "unsigned upstream",
  "enginetry.notRecorded": "not recorded",
  "enginetry.notRecordedTitle": "Forgeyard did not pin this image itself (it was written by hand): nothing was verified.",
  "enginetry.signer": "{repository} · {workflow} · {ref}",
  "enginetry.run": "the build",
  "enginetry.pinnedBy": "pinned {date} by {who}",
  "enginetry.leftOutTitle": "Left out of this copy",

  // The server's refusals (detail.code). English shows the server's own message; these carry it as {message} where it
  // has the specifics (which variant, which digest).
  "enginetry.err.check_again": "The last check does not cover this version any more. Check for newer versions again.",
  "enginetry.err.try_running": "Another copy is being made right now. Try again when it is done.",
  "enginetry.err.digest_moved": "An image or archive changed since the check, so no copy was made: {message}",
  "enginetry.err.signature_failed": "A signature did not verify, so no copy was made: {message}",
  "enginetry.err.signature_missing": "A signature is missing, so no copy was made: {message}",
  "enginetry.err.cannot_verify": "The signatures could not be verified, so no copy was made: {message}",
  "enginetry.err.registry_error": "The image registry could not be read: {message}",
  "enginetry.err.github_error": "GitHub could not be read: {message}",
  "enginetry.err.nothing_to_pin": "No variant has an image of this version, so there is nothing to copy.",
  "enginetry.err.name_taken": "That name is taken. Give the copy another name.",
  "enginetry.err.builtin_name": "That is a built-in engine's name. Give the copy another name.",
  "enginetry.err.invalid_name": "Use 1 to 40 lowercase letters, digits and dashes.",
} as const;
