#!/usr/bin/env python
# consensusTracker.py -- alerts for changes in the capacity of the tor network
# Released under the GPL v3 (http://www.gnu.org/licenses/gpl.html)

"""
Quick script for periodically checking relay count and bandwidth available on
the tor network. This sends daily reports or alerts when changes breach a given
threshold. Queries are done for each consensus, tracking stats for guards,
middle hops, and exits separately.

This determines if relays are new by checking if the fingerprint's ever been
seen before, which can take quite some time to prepopulate from scratch (on the
order of a month or so). Getting this information from karsten's metrics
project can greatly bootstrap this.
"""

import os
import sys
import time
import tarfile
import getopt
import getpass
import smtplib
from email.mime.text import MIMEText
from email.MIMEBase import MIMEBase
from email.mime.multipart import MIMEMultipart
from email import Encoders

sys.path[0] = sys.path[0][:-5]

from TorCtl import TorCtl

# TODO: remove arm dependency, pending:
# https://trac.torproject.org/projects/tor/ticket/1737
import util.torTools

# enums representing different poritons of the tor network
RELAY_GUARD, RELAY_MIDDLE, RELAY_EXIT = range(1, 4)

# defaut overwrites
DEFAULT_GMAIL_ACCOUNT = None
DEFAULT_TO_ADDRESS = None
DEFAULT_FINGERPRINTS = "./fingerprints"
DEFAULT_NS_OUTPUT = "./newRelays"

# thresholds at which alerts are sent for relay counts
HOURLY_COUNT_THRESHOLD = 20
HOURLY_BW_THRESHOLD = 10485760 # TODO: not yet sure what to set this to... trying 10 Mbit/s

OPT = "g:t:f:n:qh"
OPT_EXPANDED = ["gmail=", "to=", "fingerprints=", "nsOutput=", "quiet", "help"]
HELP_MSG = """Usage consensusTracker.py [OPTION]
Provides alerts for sharp changes in the size and capacity of the tor network.

  -g, --gmail GMAIL_ADDRESS     account used to send email alerts%s
  -t, --to EMAIL_ADDRESS        destination of email alerts%s
  -f, --fingerprints FP_FILE    location with which fingerprints are persisted%s
  -n, --nsOutput NS_OUTPUT_DIR  path to which to save ns entries for new relays%s
  -q, --quiet                   skips summary output from going to stdout
  -h, --help                    presents this help
"""
FP_WRITE_FAIL_MSG = "Unable to access '%s', fingerprints won't be persisted"

def sendViaGmail(gmailAccount, gmailPassword, toAddress, subject, msgText, msgHtml, attachment = None):
  """
  Sends an email via gmail, returning if successful or not.
  
  Arguments:
    gmailAccount  - gmail account to be used (ex, "myname@gmail.com")
    gmailPassword - gmail password
    toAddress     - address email should be sent to
    subject       - email subject
    msg           - email body text
    attachment    - path to file being attached
  """
  
  mimeMsg = MIMEMultipart('alternative')
  mimeMsg['Subject'] = subject
  mimeMsg['From'] = gmailAccount
  mimeMsg['To'] = toAddress
  
  if attachment:
    part = MIMEBase('application', "octet-stream")
    part.set_payload(open(attachment, "rb").read())
    Encoders.encode_base64(part)
    part.add_header('Content-Disposition', 'attachment; filename="%s"' % os.path.basename(attachment))
    mimeMsg.attach(part)
  
  mimeMsg.attach(MIMEText(msgText, 'plain'))
  mimeMsg.attach(MIMEText(msgHtml, 'html'))
  
  # send the message via the gmail SMTP server
  try:
    server = smtplib.SMTP('smtp.gmail.com:587')
    server.starttls()
    server.login(gmailAccount, gmailPassword)
    
    server.sendmail(gmailAccount, [toAddress], mimeMsg.as_string())
    server.quit()
    
    return True
  except smtplib.SMTPAuthenticationError:
    return False

def isExit(conn, nsEntry, default=False):
  """
  Returns if the nsEntry permits exit traffic or not. If unable to fetch the
  descriptor then this provides the default.
  
  Arguments:
  nsEntry - NetworkStatus entry for the relay
  default - results if the descriptor is unavailable
  """
  
  queryParam = "desc/id/%s" % nsEntry.idhex
  
  try:
    descEntry = conn.get_info(queryParam)[queryParam]
    
    # returns true if any accept is available before the reject-all (this errors
    # on the side of inclusiveness)
    for line in descEntry.split("\n"):
      if line == "reject *:*": return False
      elif line.startswith("accept"): return True
  except TorCtl.ErrorReply: return default

def getNextConsensus(conn, oldValidAfterDate, sleepTime = 300):
  """
  This blocks until there's a new consensus available, providing the tuple of
  the new valid after date and the new consensus.
  
  Arguments:
    oldValidAfterDate - previous valid-after date, providing the first
                        consensus if None
    sleepTime         - time to wait if a new consensus is unavailable
  """
  
  # periodically checks for a new consensus
  while True:
    try:
      newConsensus = conn.get_info("dir/status-vote/current/consensus")["dir/status-vote/current/consensus"]
      
      # checks if it's new (ie, the valid-after date has changed)
      newValidAfterDate = None
      
      for line in newConsensus.split("\n"):
        if line.startswith("valid-after"):
          newValidAfterDate = line[12:]
          break
      
      if not newValidAfterDate:
        # consensus doesn't have a valid-after date... wtf?
        print "Read consensus without a valid-after date. This really shouldn't happen so giving up."
        sys.exit(1)
      
      if oldValidAfterDate and oldValidAfterDate == newValidAfterDate:
        time.sleep(sleepTime) # consensus hasn't changed
      else:
        return (newConsensus, newValidAfterDate) # consensus has changed
    except TorCtl.ErrorReply:
      print "Failed to fetch current consensus, waiting and trying again"
      time.sleep(sleepTime)
    except TorCtl.TorCtlClosed:
      print "Connection to tor is closed"
      sys.exit()

def getSizeLabel(bits, decimal = 0):
  """
  Converts byte count into label in its most significant units, for instance
  7500 bytes would return "56 KBits".
  """
  
  format = "%%.%if" % decimal
  if bits >= 1073741824: return (format + " GBit/s") % (bits / 1073741824.0)
  elif bits >= 1048576: return (format + " MBit/s") % (bits / 1048576.0)
  elif bits >= 1024: return (format + " KBit/s") % (bits / 1024.0)
  else: return "%i bits/s" % bits

class Sampling:
  """
  Consensus attributes for a given time period we're concerned with for
  generating alerts.
  """
  
  def __init__(self, conn, validAfter, allRelays, newRelays):
    """
    Creates a new sampling.
    
    Arguments:
      validAfter - consensus' starting date (ex. "2010-07-18 10:00:00")
      allRelays  - listing of all ns entries in the consensus
      newRelays  - listing of ns entries with a fingerprint we haven't seen
                   before
    """
    
    self.validAfter = validAfter
    
    # constructs mappings of relayType -> [nsEntry list]
    types = (RELAY_GUARD, RELAY_MIDDLE, RELAY_EXIT)
    self.allRelays = dict([(relayType, []) for relayType in types])
    self.newRelays = dict([(relayType, []) for relayType in types])
    
    for uncategorized, categorized in ((allRelays, self.allRelays), (newRelays, self.newRelays)):
      for nsEntry in uncategorized:
        relayType = RELAY_MIDDLE
        if isExit(conn, nsEntry): relayType = RELAY_EXIT
        elif "Guard" in nsEntry.flags: relayType = RELAY_GUARD
        categorized[relayType].append(nsEntry)
  
  def getValidAfter(self):
    return self.validAfter
  
  def getCount(self, relayType, newOnly=True):
    if newOnly: return len(self.newRelays[relayType])
    else: return len(self.allRelays[relayType])
  
  def getCounts(self, newOnly=True):
    guardCount = self.getCount(RELAY_GUARD, newOnly)
    middleCount = self.getCount(RELAY_MIDDLE, newOnly)
    exitCount = self.getCount(RELAY_EXIT, newOnly)
    return (guardCount, middleCount, exitCount)
  
  # TODO: Descriptor bandwidths (ie, the observed bandwidth) tend to be a 
  # better metric since they aren't tampered by the bandwidth authorities.
  def getBandwidth(self, relayType, newOnly=True):
    totalBandwidth = 0
    
    relaySet = self.newRelays[relayType] if newOnly else self.allRelays[relayType]
    for nsEntry in relaySet:
      if nsEntry.bandwidth: totalBandwidth += nsEntry.bandwidth
    
    return totalBandwidth
  
  def getTotalBandwidth(self, newOnly=True):
    totalBandwidth = 0
    totalBandwidth = self.getBandwidth(RELAY_GUARD, newOnly)
    totalBandwidth += self.getBandwidth(RELAY_MIDDLE, newOnly)
    totalBandwidth += self.getBandwidth(RELAY_EXIT, newOnly)
    return totalBandwidth
  
  def getSummary(self):
    """
    Provides a single line summary like:
    2010-07-18 10:00:00 - 941/1732/821 relays (8/12/4 are new, 153 MB added bandwidth)
    """
    
    relayCounts = "%i/%i/%i relays" % (self.getCounts(False))
    newRelayCounts = "%i/%i/%i are new" % (self.getCounts(True))
    newBandwidth = getSizeLabel(self.getTotalBandwidth())
    return "%s - %s (%s, %s added bandwidth)" % (self.getValidAfter(), relayCounts, newRelayCounts, newBandwidth)

def monitorConsensus():
  gmailAccount, gmailPassword = DEFAULT_GMAIL_ACCOUNT, ""
  toAddress = DEFAULT_TO_ADDRESS
  seenFingerprintsPath = DEFAULT_FINGERPRINTS
  nsOutputPath = DEFAULT_NS_OUTPUT
  isQuiet = False
  
  # parses user input, noting any issues
  try:
    opts, args = getopt.getopt(sys.argv[1:], OPT, OPT_EXPANDED)
  except getopt.GetoptError, exc:
    print str(exc) + " (for usage provide --help)"
    sys.exit()
  
  for opt, arg in opts:
    if opt in ("-g", "--gmail"): gmailAccount = arg
    elif opt in ("-t", "--to"): toAddress = arg
    elif opt in ("-f", "--fingerprints"): seenFingerprintsPath = arg
    elif opt in ("-n", "--nsOutput"): nsOutputPath = arg
    elif opt in ("-q", "--quiet"): isQuiet = True
    elif opt in ("-h", "--help"):
      # notes default values if they exist
      gmailAcctLabel = " (%s)" % gmailAccount if gmailAccount else ""
      toAddrLabel = " (%s)" % toAddress if toAddress else ""
      seenFpLabel = " (%s)" % seenFingerprintsPath if seenFingerprintsPath else ""
      nsOutputLabel = " (%s)" % nsOutputPath if nsOutputPath else ""
      
      print HELP_MSG % (gmailAcctLabel, toAddrLabel, seenFpLabel, nsOutputLabel)
      sys.exit()
  
  # ns output path is a directory, and later expected to end with a slash
  if nsOutputPath and not nsOutputPath.endswith("/"):
    nsOutputPath += "/"
  
  # fetches gmail password if we're sending email alerts
  isEmailUsed = gmailAccount and toAddress
  if isEmailUsed: gmailPassword = getpass.getpass("GMail Password: ")
  
  # get a control port connection
  conn = util.torTools.connect()
  if conn == None:
    print "Unable to connect to control port"
    sys.exit(1)
  
  # prepopulate seenFingerprints with past entries if available
  seenFingerprints = set()
  if seenFingerprintsPath and os.path.exists(seenFingerprintsPath):
    try:
      seenFingerprintsFile = open(seenFingerprintsPath, "r")
      
      for entry in seenFingerprintsFile:
        seenFingerprints.add(entry.upper().strip())
      
      seenFingerprintsFile.close()
    except IOError:
      print "Unable to prepopulate fingerprints from %s" % seenFingerprintsPath
  
  seenFingerprintsFile = None
  if seenFingerprintsPath:
    try: seenFingerprintsFile = open(seenFingerprintsPath, "a")
    except IOError: print FP_WRITE_FAIL_MSG % seenFingerprintsPath
  
  tick = 0 # number of consensuses processed
  samplings = []
  validAfterDate = None # the 'valid-after' time of the last consensus we've processed
  
  while True:
    tick += 1
    
    # fetches the consensus, blocking until a new one's available
    newConsensus, validAfterDate = getNextConsensus(conn, validAfterDate)
    nsEntries = TorCtl.parse_ns_body(newConsensus)
    
    # determines which entries are new and records the seen fingerprints
    newEntries = []
    for nsEntry in nsEntries:
      if not nsEntry.idhex in seenFingerprints:
        newEntries.append(nsEntry)
        seenFingerprints.add(nsEntry.idhex)
        
        if seenFingerprintsFile:
          try:
            seenFingerprintsFile.write(nsEntry.idhex + "\n")
          except IOError:
            print FP_WRITE_FAIL_MSG % seenFingerprintsPath
            seenFingerprintsFile = None
    
    newSampling = Sampling(conn, validAfterDate, nsEntries, newEntries)
    
    # check if we broke any thresholds (currently just checking hourly exit stats)
    countAlert = newSampling.getCount(RELAY_EXIT, True) > HOURLY_COUNT_THRESHOLD
    bwAlert = newSampling.getBandwidth(RELAY_EXIT, True) > HOURLY_BW_THRESHOLD
    
    samplings.insert(0, newSampling)
    if len(samplings) > 168: samplings.pop()
    
    # writes new ns entries
    if nsOutputPath:
      nsContents = ""
      entryDir = nsOutputPath + newSampling.getValidAfter().split(" ")[0] + "/"
      entryFilename = newSampling.getValidAfter().split(" ")[1] + ".txt"
      
      for label, relayType in (("Exits:", RELAY_EXIT), ("Middle:", RELAY_MIDDLE), ("Guards:", RELAY_GUARD)):
        nsContents += label + "\n"
        nsContents += "-" * 40 + "\n"
        for nsEntry in newSampling.newRelays[relayType]:
          nsContents += "%s (%s:%s)\n" % (nsEntry.idhex, nsEntry.ip, nsEntry.orport)
          nsContents += "    nickname: %s\n    flags: %s\n\n" % (nsEntry.nickname, ", ".join(nsEntry.flags))
      
      try:
        # make ns entries directory if it doesn't already exist
        if not os.path.exists(entryDir): os.makedirs(entryDir)
        
        # creates subdirectory for each date, then file named after the time
        nsFile = open(entryDir + entryFilename, "w")
        nsFile.write(nsContents)
        nsFile.close()
      except IOError:
        print "Unable to access '%s', network status summaries won't be persisted" % (entryDir + entryFilename)
        nsOutputPath = None
    
    # prints results to terminal, ex:
    # 7. 2010-07-18 10:00:00 - 941/1732/821 relays (8/12/4 are new, 153 MB / 215 MB / 48 MB added bandwidth)
    if not isQuiet:
      print "%i. %s" % (tick, newSampling.getSummary())
      if countAlert: print "  *count threshold broken*"
      if bwAlert: print "  *bandwidth threshold broken*"
    
    if countAlert or bwAlert or (tick % 24 == 0):
      currentTime = time.strftime("%H:%M", time.localtime(time.time()))
      currentDate = time.strftime("%m/%d/%Y", time.localtime(time.time()))
      
      if countAlert:
        subject = "Alert: Relay Count Threshold Broken"
        noticeBody = "The relay count threshold was broken today at %s (%s) with the addition of %i new exits (the current threshold is set at %i)."
        noticeMsg = noticeBody % (currentTime, currentDate, newSampling.getCount(RELAY_EXIT), HOURLY_COUNT_THRESHOLD)
      elif bwAlert:
        subject = "Alert: Relay Bandwidth Threshold Broken"
        noticeBody = "The relay bandwidth threshold was broken today at %s (%s) with the addition of %s of new exit capacity (the current threshold is set at %i)."
        noticeMsg = noticeBody % (currentTime, currentDate, getSizeLabel(newSampling.getBandwidth(RELAY_EXIT)), getSizeLabel(HOURLY_BW_THRESHOLD))
      else:
        subject = "Daily Consensus Report for %s" % currentDate
        noticeMsg = "At present there's no breaches to report. See below for a summary of consensus additions."
      
      greetingMsg = "Greetings from your friendly consensus monitoring daemon. %s" % noticeMsg
      
      # constructs the plain text message
      msgText = greetingMsg + "\n"
      msgText += "-" * 80 + "\n\n"
      
      for sampling in samplings:
        msgText += sampling.getSummary() + "\n"
      
      # constructs the html message
      msgHtml = """<html>
  <head></head>
  <body>
    <p>%s</p>
    <hr />
    <table border="1">
      <tr>
        <td></td>
        <td colspan="3" bgcolor="green"><b>Guards</b></td>
        <td colspan="3" bgcolor="yellow"><b>Middle-Only</b></td>
        <td colspan="3" bgcolor="red"><b>Exits</b></td>
      </tr>
      
      <tr>
        <td><b>Date:</b></td>
        <td bgcolor="green"><b>Count:&nbsp;</b></td>
        <td bgcolor="green"><b>New:&nbsp;</b></td>
        <td bgcolor="green"><b>Bandwidth:&nbsp;</b></td>
        <td bgcolor="yellow"><b>Count:&nbsp;</b></td>
        <td bgcolor="yellow"><b>New:&nbsp;</b></td>
        <td bgcolor="yellow"><b>Bandwidth:&nbsp;</b></td>
        <td bgcolor="red"><b>Count:&nbsp;</b></td>
        <td bgcolor="red"><b>New:&nbsp;</b></td>
        <td bgcolor="red"><b>Bandwidth:&nbsp;</b></td>
      </tr>
      
""" % greetingMsg
      
      dailyCellEntry = """
      <tr>
        <td>%s</td>
        <td bgcolor="#44FF44"><b>%s</b></td>
        <td bgcolor="#44FF44"><b>%s</b></td>
        <td bgcolor="#44FF44"><b>%s</b></td>
        <td bgcolor="#FFFF44"><b>%s</b></td>
        <td bgcolor="#FFFF44"><b>%s</b></td>
        <td bgcolor="#FFFF44"><b>%s</b></td>
        <td bgcolor="#FF4444"><b>%s</b></td>
        <td bgcolor="#FF4444"><b>%s</b></td>
        <td bgcolor="#FF4444"><b>%s</b></td>
      </tr>
"""
      
      hourlyCellEntry = dailyCellEntry.replace("<b>", "").replace("</b>", "").replace("44", "88")
      
      # make a mapping of date => [samplings]
      datesToSamplings = {}
      
      for sampling in samplings:
        consensusDate = sampling.getValidAfter().split(" ")[0]
        
        if consensusDate in datesToSamplings:
          datesToSamplings[consensusDate].append(sampling)
        else:
          datesToSamplings[consensusDate] = [sampling]
      
      dates = list(datesToSamplings.keys())
      dates.sort()
      
      for date in dates:
        # stores to get the daily sums later
        gCounts, gNew, gBw = [], [], []
        mCounts, mNew, mBw = [], [], []
        eCounts, eNew, eBw = [], [], []
        
        hourlyEntries = ""
        for sampling in datesToSamplings[date]:
          consensusTime = sampling.getValidAfter().split(" ")[1]
          
          gCounts.append(sampling.getCount(RELAY_GUARD, False))
          gNew.append(sampling.getCount(RELAY_GUARD))
          gBw.append(sampling.getBandwidth(RELAY_GUARD))
          
          mCounts.append(sampling.getCount(RELAY_MIDDLE, False))
          mNew.append(sampling.getCount(RELAY_MIDDLE))
          mBw.append(sampling.getBandwidth(RELAY_MIDDLE))
          
          eCounts.append(sampling.getCount(RELAY_EXIT, False))
          eNew.append(sampling.getCount(RELAY_EXIT))
          eBw.append(sampling.getBandwidth(RELAY_EXIT))
          
          hourlyEntries += hourlyCellEntry % (consensusTime, gCounts[-1], gNew[-1], getSizeLabel(gBw[-1]), mCounts[-1], mNew[-1], getSizeLabel(mBw[-1]), eCounts[-1], eNew[-1], getSizeLabel(eBw[-1]))
        
        # append daily summary then hourly entries
        msgHtml += dailyCellEntry % (date, sum(gCounts), sum(gNew), getSizeLabel(sum(gBw)), sum(mCounts), sum(mNew), getSizeLabel(sum(mBw)), sum(eCounts), sum(eNew), getSizeLabel(sum(eBw)))
        msgHtml += hourlyEntries
      
      msgHtml += """    </table>
  </body>
</html>"""
      
      # creates a tarball with the ns entries directory
      attachment = None
      if nsOutputPath:
        try:
          t = tarfile.open(nsOutputPath[:-1] + ".tar.gz", mode = 'w:gz')
          t.add(nsOutputPath)
          t.close()
          attachment = nsOutputPath[:-1] + ".tar.gz"
        except IOError:
          print "Unable to email archive with new relays."
      
      sendViaGmail(gmailAccount, gmailPassword, toAddress, subject, msgText, msgHtml, attachment)

if __name__ == '__main__':
  monitorConsensus()

