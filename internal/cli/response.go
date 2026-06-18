//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

package cli

import "fmt"

// ResponseIf is the unified result of a command execution. Command functions
// return a ResponseIf and the central loop calls PrintOut to render it, instead
// of each command printing inline.
type ResponseIf interface {
	Type() string
	PrintOut()
	TimeCost() float64
	SetOutputFormat(format OutputFormat)
}

// CommonResponse carries a list payload (e.g. LIST commands).
type CommonResponse struct {
	Code         int                      `json:"code"`
	Data         []map[string]interface{} `json:"data"`
	Message      string                   `json:"message"`
	Duration     float64
	outputFormat OutputFormat
}

func (r *CommonResponse) Type() string                        { return "common" }
func (r *CommonResponse) TimeCost() float64                   { return r.Duration }
func (r *CommonResponse) SetOutputFormat(format OutputFormat) { r.outputFormat = format }

func (r *CommonResponse) PrintOut() {
	if r.Code == 0 {
		PrintTableSimpleByFormat(r.Data, r.outputFormat)
	} else {
		fmt.Println("ERROR")
		fmt.Printf("%d, %s\n", r.Code, r.Message)
	}
}

// CommonDataResponse carries a single-object payload (e.g. SHOW / GENERATE).
type CommonDataResponse struct {
	Code         int                    `json:"code"`
	Data         map[string]interface{} `json:"data"`
	Message      string                 `json:"message"`
	Duration     float64
	outputFormat OutputFormat
}

func (r *CommonDataResponse) Type() string                        { return "show" }
func (r *CommonDataResponse) TimeCost() float64                   { return r.Duration }
func (r *CommonDataResponse) SetOutputFormat(format OutputFormat) { r.outputFormat = format }

func (r *CommonDataResponse) PrintOut() {
	if r.Code == 0 {
		PrintTableSimpleByFormat([]map[string]interface{}{r.Data}, r.outputFormat)
	} else {
		fmt.Println("ERROR")
		fmt.Printf("%d, %s\n", r.Code, r.Message)
	}
}

// SimpleResponse carries no payload (e.g. DROP / GRANT / ALTER).
type SimpleResponse struct {
	Code         int    `json:"code"`
	Message      string `json:"message"`
	Duration     float64
	outputFormat OutputFormat
}

func (r *SimpleResponse) Type() string                        { return "simple" }
func (r *SimpleResponse) TimeCost() float64                   { return r.Duration }
func (r *SimpleResponse) SetOutputFormat(format OutputFormat) { r.outputFormat = format }

func (r *SimpleResponse) PrintOut() {
	if r.Code == 0 {
		fmt.Println("SUCCESS")
	} else {
		fmt.Println("ERROR")
		fmt.Printf("%d, %s\n", r.Code, r.Message)
	}
}

// RegisterResponse is the result of REGISTER USER.
type RegisterResponse struct {
	Code         int    `json:"code"`
	Message      string `json:"message"`
	Duration     float64
	outputFormat OutputFormat
}

func (r *RegisterResponse) Type() string                        { return "register" }
func (r *RegisterResponse) TimeCost() float64                   { return r.Duration }
func (r *RegisterResponse) SetOutputFormat(format OutputFormat) { r.outputFormat = format }

func (r *RegisterResponse) PrintOut() {
	if r.Code == 0 {
		fmt.Println("Register successfully")
	} else {
		fmt.Println("ERROR")
		fmt.Printf("%d, %s\n", r.Code, r.Message)
	}
}

// BenchmarkResponse is the result of a benchmarked command.
type BenchmarkResponse struct {
	Code         int     `json:"code"`
	Duration     float64 `json:"duration"`
	SuccessCount int     `json:"success_count"`
	FailureCount int     `json:"failure_count"`
	Concurrency  int
	outputFormat OutputFormat
}

func (r *BenchmarkResponse) Type() string                        { return "benchmark" }
func (r *BenchmarkResponse) TimeCost() float64                   { return r.Duration }
func (r *BenchmarkResponse) SetOutputFormat(format OutputFormat) { r.outputFormat = format }

func (r *BenchmarkResponse) PrintOut() {
	if r.Code != 0 {
		fmt.Printf("ERROR, Code: %d\n", r.Code)
		return
	}

	iterations := r.SuccessCount + r.FailureCount
	if r.Concurrency <= 1 {
		if iterations <= 1 {
			fmt.Printf("Latency: %fs\n", r.Duration)
		} else {
			fmt.Printf("Latency: %fs, QPS: %.1f, SUCCESS: %d, FAILURE: %d\n", r.Duration, float64(iterations)/r.Duration, r.SuccessCount, r.FailureCount)
		}
	} else {
		fmt.Printf("Concurrency: %d, Latency: %fs, QPS: %.1f, SUCCESS: %d, FAILURE: %d\n", r.Concurrency, r.Duration, float64(iterations)/r.Duration, r.SuccessCount, r.FailureCount)
	}
}

// BenchmarkRawResponse carries the raw per-iteration responses produced by
// RequestWithIterations. It satisfies ResponseIf so command functions can
// return it in benchmark mode; the benchmark runner consumes ResponseList /
// Duration directly and computes its own stats, so PrintOut is a no-op.
type BenchmarkRawResponse struct {
	Duration     float64
	ResponseList []*Response
	outputFormat OutputFormat
}

func (r *BenchmarkRawResponse) Type() string                        { return "benchmark_raw" }
func (r *BenchmarkRawResponse) TimeCost() float64                   { return r.Duration }
func (r *BenchmarkRawResponse) SetOutputFormat(format OutputFormat) { r.outputFormat = format }
func (r *BenchmarkRawResponse) PrintOut()                           {}

// KeyValueResponse carries a single key/value pair (e.g. SHOW VARIABLE).
type KeyValueResponse struct {
	Code         int    `json:"code"`
	Key          string `json:"key"`
	Value        string `json:"data"`
	Duration     float64
	outputFormat OutputFormat
}

func (r *KeyValueResponse) Type() string                        { return "data" }
func (r *KeyValueResponse) TimeCost() float64                   { return r.Duration }
func (r *KeyValueResponse) SetOutputFormat(format OutputFormat) { r.outputFormat = format }

func (r *KeyValueResponse) PrintOut() {
	if r.Code == 0 {
		PrintTableSimpleByFormat([]map[string]interface{}{{
			"key":   r.Key,
			"value": r.Value,
		}}, r.outputFormat)
	} else {
		fmt.Println("ERROR")
		fmt.Printf("%d\n", r.Code)
	}
}
