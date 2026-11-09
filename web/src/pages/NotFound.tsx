import { Link } from "react-router";

import { PageHeader } from "../components/ui";

export function NotFoundPage() {
  return (
    <>
      <PageHeader index="404" title="Nothing here" lead="This page does not exist." />
      <Link className="btn" to="/machines">Go to Machines</Link>
    </>
  );
}

export function AdminsOnly() {
  return (
    <>
      <PageHeader index="403" title="Admins only" lead="Your role cannot open this page. Ask an admin if you need it." />
      <Link className="btn" to="/machines">Go to Machines</Link>
    </>
  );
}
