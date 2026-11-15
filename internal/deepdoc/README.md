# Optional Go DLA client

`NewClientWithURL(baseURL).DLA(ctx, images)` sends one multipart `request` JPEG
part per image to `/predict` and reads `bboxes` rows shaped as
`[left, top, right, bottom, score, class_index]`. The original interface returns
a flat region list, with one zero-value result for an image with valid empty
detections. Network errors,
HTTP failures, malformed boxes and exhausted retries remain errors.

The client bounds each response at 20 MiB. Its default HTTP client has an
18 second timeout, and requests use the caller's context. The original options
can override the HTTP client and retry count. The default retries transient
failures at most three times; redirects
and 4xx responses do not retry. Error messages do not echo server response bodies.

`NewClient` reads `DEEPDOC_URL`, then `TENSORRT_DLA_SVR`. Configuration alone does
not activate a parser. This checkout has no verified deployment DLA service;
tests cover the upstream multipart wire contract using an owned HTTP server.
No production/real-inference acceptance is implied by those tests. OCR and TSR
have no verified remote contract in this integration; they explicitly return
`ErrNoRemoteEndpoint` and do not pretend to run local ONNX inference.
