# Change History

## [0.4.0] - 2025-11-12

* Drop support for Python 3.8 and 3.9
* Drop `websockets` dependency
* Make the `websockets` module an example
* Fix typing of AsyncGenerators
* Cleanup unclosed memory object streams to address anyio 4.4.0+ resource warnings

## [0.3.0] - 2023-11-1

* Bump supported version of `anyio` to 4.x

## [0.2.0] - 2023-02-22

* Exceptions are serialized with the full traceback text, instead of simply `repr(exc)`.

## [0.1.1] - 2021-09-22

* Added `py.typed` file for PEP 561 compatibility.

## [0.1.0] - 2021-09-20

* The initial release
