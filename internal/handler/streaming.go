package handler

import (
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
)

// disableWriteDeadlineForSSE removes only this response's server write timeout.
// Call before waiting for the first model frame. It deliberately writes no
// headers, so preparation and authorization failures can still return JSON.
// ResponseRecorder and middleware without deadline support return ErrNotSupported;
// streaming remains usable on those writers without changing its error contract.
func disableWriteDeadlineForSSE(c *gin.Context) {
	_ = http.NewResponseController(c.Writer).SetWriteDeadline(time.Time{})
}
