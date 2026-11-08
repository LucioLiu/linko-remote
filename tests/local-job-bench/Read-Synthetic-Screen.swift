import Foundation
import Vision
import AppKit
guard CommandLine.arguments.count == 2, let source = NSImage(contentsOfFile: CommandLine.arguments[1]), let cg = source.cgImage(forProposedRect: nil,context:nil,hints:nil) else {exit(2)}
let req=VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.usesLanguageCorrection = false
req.recognitionLanguages = ["en-US"]
try VNImageRequestHandler(cgImage:cg,options:[:]).perform([req])
let data:[[String:Any]]=(req.results ?? []).compactMap { x in
 guard let y=x.topCandidates(1).first else{return nil}
 let b=x.boundingBox
 return ["text":y.string,"x":Int(b.midX*CGFloat(cg.width)),"y":Int((1-b.midY)*CGFloat(cg.height))]
}
FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject:data))
