# gkm-metaschema


Tools and scripts for parsing the GA4GH Genomic Knowledge Standards (GKS) metaschemas.
The metaschema processor (MSP) converts
[JSON Schema Version 2020-12](json-schema.org/draft/2020-12/schema) in YAML to
reStructuredText (RST) and JSON files.

Currently used in:

* [GKM-Core](https://github.com/ga4gh/gkm-core)
* [VRS](https://github.com/ga4gh/vrs)
* [VA-Spec](https://github.com/ga4gh/va-spec/)
* [Cat-VRS](https://github.com/ga4gh/cat-vrs)

## Metaschema processing model

A `*-source.yaml` document is JSON Schema 2020-12 with a few GKS conventions
the processor expands into standard JSON Schema. In brief:

* **Classes** are abstract (`abstract: true`) or concrete. A concrete class is
  either **inherited** (`inherits:` a parent, with members under
  `properties` / `required`) or **composed** (a top-level
  `allOf`/`anyOf`/`oneOf` — e.g. the VA/cat-vrs profiles and recipes). The
  processor injects `type: object`, and every class — abstract included — is
  emitted as its own schema.
* **Inheritance** copies the parent's `properties`/`required` into the child,
  **superclass-first**. A subclass **specializes** an inherited property by
  redeclaring it under the **same name**; the legacy `extends` keyword is
  **removed** and now raises an error.
* **Schema covariance (Liskov):** a parent schema must always validate a
  subclass instance. A subclass may narrow properties (add constraints), refine
  descriptions/comments/array sizes, and add properties — but it may **not**
  rename an inherited property or change its `type`/`const`/`default`
  (violations raise an error).
* **References** to an abstract class stay direct `$ref`s (no expansion into a
  `oneOf` of descendants).
* **Closure** (with `strict: true`): concrete classes get
  `additionalProperties: false`; `allOf`/`anyOf`/`oneOf`-composed classes get
  `unevaluatedProperties: false`; abstract classes are left open.

The full, authoritative description — including known limitations — is in
**[METASCHEMA_BEHAVIOR.md](METASCHEMA_BEHAVIOR.md)**. Keep that document in sync
when processor behavior changes.

<details>
<summary><b>📊 Visual overview</b> — how the processor works (click to expand)</summary>

<br>

<a href="docs/assets/msp-explainer-light.png">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/msp-explainer-dark.png">
    <img
      width="900"
      alt="The metaschema processor explained as a compiler: a DRY *-source.yaml is compiled — inlining inheritance, resolving $refs, injecting type: object — into self-contained per-class JSON Schemas plus RST docs, subject to type-system guarantees (base members first, Liskov substitution, concrete types sealed / abstract left open, abstract types not instantiated)."
      src="docs/assets/msp-explainer-light.png">
  </picture>
</a>

_Click the image for full resolution, or open the self-contained interactive
version at [`docs/msp-explainer.html`](docs/msp-explainer.html)._

</details>

## Installing for development

### Prerequisites

* Python 3.12: We recommend using [pyenv](https://github.com/pyenv/pyenv).

### Installation Steps

Fork the repo at <https://github.com/ga4gh/gkm-metaschema>, and initialize a development
environment.

    git clone git@github.com:YOUR_GITHUB_ID/gkm-metaschema.git
    cd gkm-metaschema
    make devready
    source venv/3.12/bin/activate


Set up `pre-commit` hooks:

    pre-commit install


### Testing

To run the tests:

    make test

## Usage

### File Hierarchy

The metaschema processor expects the following hierarchy:

    ├── docs
    │   ├── source
    │   |   ├── ...
    │   ├── Makefile
    ├── schema
    │   ├──gkm_schema
    │   |   ├── gkm-schema-source.yaml
    │   |   ├── Makefile
    │   |   ├── prune.mk
    │   ├── Makefile

* `docs`: [Sphinx](https://www.sphinx-doc.org/en/master/index.html) documentation
    directory. **Must** be named `docs`.
  * `source`: Directory containing documentation written in reStructuredText and Sphinx
    configuration. **Must** be named `source`.
  * `Makefile`: Commands to create the reStructuredText files.
    This file should not change across GKS projects.
* `schema`: Schema directory. Can also contain submodules for other GKS product schemas.
  * `gkm_schema`: Schema directory for GKS product. The directory name should reflect
    the product, e.g. `vrs`.
    * `gkm-schema-source.yaml`: Source document for the JSON Schema 2020-12. The file name
          should reflect the standard, e.g. `vrs-source.yaml`. The  file name **must** end
          with `-source.yaml`.
    * `Makefile`: Commands to create the reStructuredText and JSON files.
          This file should not change across GKS projects.
    * `prune.mk`: Cleanup of files in `def` and `json` directories based on source document.
          This file should not change across GKS projects.
  * `Makefile`: Commands to create the reStructuredText and JSON files.

### Contributing to the schema

To create the corresponding `def` (reStructuredText) and `json` files after making
changes to the source document, from the _schema_ directory:

        make all

The file structure will now look like:

    ├── schema
    │   ├──gkm_schema
    |   |   ├── def
    │   |   |   ├── ...
    |   |   ├── json
    │   |   |   ├── ...
    │   |   ├── gkm-schema-source.yaml
    │   |   ├── Makefile
    │   |   ├── prune.mk
    │   ├── Makefile

### Contributing to the docs

GKS specification documentation is written in reStructuredText and located in
`docs/source`.

To build documentation locally, you must install [entr](https://eradman.com/entrproject/):

    brew install entr

Then from the _docs_ directory:

    make clean watch &

Then, open `docs/build/html/index.html`. The above make command should build docs when
the source changes.

> **NOTE**: Some types of changes require recleaning and building.
