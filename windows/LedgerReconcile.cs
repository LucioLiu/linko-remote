using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using Microsoft.VisualBasic.FileIO;

public sealed class LedgerBatch {
 public string Batch { get; internal set; }
 public int ExpectedRows { get; internal set; }
 public int ActualRows { get; internal set; }
 public int DiscrepancyCount { get { return Rows.Count; } }
 public long TotalDeltaCents { get; internal set; }
 internal readonly List<string> Rows = new List<string>();
}

public static class LedgerReconcile {
 public const int MaxFileBytes = 524288;
 public const int MaxBatches = 24;
 static readonly UTF8Encoding Utf8 = new UTF8Encoding(false,true);
 static readonly CultureInfo Invariant = CultureInfo.InvariantCulture;
 public static bool ValidBatch(string batch) {
  return batch != null && Regex.IsMatch(batch,@"\A[0-9]{2}\z");
 }
 public static string Sha(byte[] bytes) {
  using(var hash=SHA256.Create())return BitConverter.ToString(hash.ComputeHash(bytes)).Replace("-","").ToLowerInvariant();
 }
 static Dictionary<string,long> ReadLedger(string file,out int rows) {
  rows=0;
  if((new FileInfo(file)).Length>MaxFileBytes)throw new InvalidDataException("LEDGER_TOO_LARGE");
  var totals=new Dictionary<string,long>(StringComparer.Ordinal);
  string text=Utf8.GetString(File.ReadAllBytes(file)).TrimStart('\uFEFF');
  using(var parser=new TextFieldParser(new StringReader(text))){
   parser.TextFieldType=FieldType.Delimited;parser.SetDelimiters(",");
   parser.HasFieldsEnclosedInQuotes=true;parser.TrimWhiteSpace=false;
   string[] header=parser.ReadFields();
   if(header==null || header.Length!=2 || header[0]!="id" || header[1]!="amount")throw new InvalidDataException("LEDGER_HEADER");
   while(!parser.EndOfData){
    string[] row=parser.ReadFields();
    if(++rows>20000 || row==null || row.Length!=2 || String.IsNullOrWhiteSpace(row[0]) || row[0].Length>128 ||
       Regex.IsMatch(row[0],@"[\x00-\x1F]") || row[1].Length>24 ||
       !Regex.IsMatch(row[1],@"\A[+-]?(?:[0-9]+(?:\.[0-9]{1,2})?|\.[0-9]{1,2})\z"))
     throw new InvalidDataException("LEDGER_ROW");
    long cents=checked((long)(Decimal.Parse(row[1],NumberStyles.AllowLeadingSign|NumberStyles.AllowDecimalPoint,Invariant)*100m));
    long prior;totals.TryGetValue(row[0],out prior);totals[row[0]]=checked(prior+cents);
   }
  }
  return totals;
 }
 static string Csv(string value) {
  return value.IndexOfAny(new char[]{',','"','\r','\n'})<0?value:"\""+value.Replace("\"","\"\"")+"\"";
 }
 public static LedgerBatch ProcessBatch(string inputDirectory,string batch) {
  if(!ValidBatch(batch))throw new InvalidDataException("INVALID_BATCH");
  int er,ar;
  var expected=ReadLedger(Path.Combine(inputDirectory,"expected-"+batch+".csv"),out er);
  var actual=ReadLedger(Path.Combine(inputDirectory,"actual-"+batch+".csv"),out ar);
  var result=new LedgerBatch{Batch=batch,ExpectedRows=er,ActualRows=ar};
  var ids=new SortedSet<string>(expected.Keys,StringComparer.Ordinal);ids.UnionWith(actual.Keys);
  foreach(string id in ids){
   long e,a;bool he=expected.TryGetValue(id,out e),ha=actual.TryGetValue(id,out a);
   if(he && ha && e==a)continue;
   result.TotalDeltaCents=checked(result.TotalDeltaCents+checked(a-e));
   result.Rows.Add(String.Join(",",new string[]{batch,Csv(id),he?e.ToString(Invariant):"",ha?a.ToString(Invariant):"",!ha?"missing_actual":!he?"missing_expected":"different"}));
  }
  return result;
 }
 [DataContract] sealed class BatchReport {
  [DataMember] public string batch;
  [DataMember] public int expected_rows,actual_rows,discrepancy_count;
  [DataMember] public long total_delta_cents;
 }
 [DataContract] sealed class Report {
  [DataMember] public List<BatchReport> batches=new List<BatchReport>();
  [DataMember] public int batch_count, discrepancy_count, csv_bytes;
  [DataMember] public long total_delta_cents;
  [DataMember] public string csv_sha256;
 }
 public static string Export(string outputDirectory,LedgerBatch[] batches) {
  if(batches==null || batches.Length<1 || batches.Length>MaxBatches)throw new InvalidDataException("INVALID_BATCH_COUNT");
  var ordered=new SortedDictionary<string,LedgerBatch>(StringComparer.Ordinal);
  foreach(var batch in batches)ordered.Add(batch.Batch,batch);
  var report=new Report{batch_count=ordered.Count};
  var csv=new StringBuilder("batch,id,expected_cents,actual_cents,status\r\n");
  foreach(var item in ordered.Values){
   report.batches.Add(new BatchReport{batch=item.Batch,expected_rows=item.ExpectedRows,actual_rows=item.ActualRows,
                                      discrepancy_count=item.DiscrepancyCount,total_delta_cents=item.TotalDeltaCents});
   report.discrepancy_count+=item.DiscrepancyCount;
   report.total_delta_cents=checked(report.total_delta_cents+item.TotalDeltaCents);
   foreach(string row in item.Rows){
    csv.Append(row).Append("\r\n");if(csv.Length>2097152)throw new InvalidDataException("RESULT_TOO_LARGE");
   }
  }
  byte[] data=Utf8.GetBytes(csv.ToString());
  if(data.Length>2097152)throw new InvalidDataException("RESULT_TOO_LARGE");
  Directory.CreateDirectory(outputDirectory);
  string output=Path.Combine(outputDirectory,"result.csv"),temp=output+"."+Guid.NewGuid().ToString("N")+".tmp";
  try{
   File.WriteAllBytes(temp,data);
   if(File.Exists(output))File.Replace(temp,output,null);else File.Move(temp,output);
  }finally{if(File.Exists(temp))File.Delete(temp);}
  byte[] saved=File.ReadAllBytes(output);report.csv_sha256=Sha(saved);report.csv_bytes=saved.Length;
  if(saved.Length!=data.Length || report.csv_sha256!=Sha(data))throw new IOException("EXPORT_READBACK_MISMATCH");
  using(var json=new MemoryStream()){
   new DataContractJsonSerializer(typeof(Report)).WriteObject(json,report);return Utf8.GetString(json.ToArray());
  }
 }
 public static string Run(string inputDirectory,string outputDirectory,string[] batches) {
  if(batches==null || batches.Length<1 || batches.Length>MaxBatches)throw new InvalidDataException("INVALID_BATCH_COUNT");
  var results=new List<LedgerBatch>();
  foreach(string batch in batches)results.Add(ProcessBatch(inputDirectory,batch));
  return Export(outputDirectory,results.ToArray());
 }
}
