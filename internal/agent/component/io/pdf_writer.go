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

// Package io contains document writers. PDF output is unavailable until
// a real font/rendering configuration is supplied; it never returns fake bytes.
package io

import "errors"

type PDFOptions struct {
	FontSize       int
	HeaderText     string
	FooterText     string
	WatermarkText  string
	AddPageNumbers bool
	AddTimestamp   bool
	FontFamily     string
}

var ErrPDFFontNotConfigured = errors.New("PDF output unavailable: no font/rendering configuration")

func WritePDF(_ string, _ PDFOptions) ([]byte, error) {
	return nil, ErrPDFFontNotConfigured
}
